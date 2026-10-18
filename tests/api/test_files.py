"""Photos on answers and file access (07-CONTEXT D-20; VER-03, VER-04, FLOW-11; 04-API-CONTRACT "Answer photos and
file access", `GET /api/signals/{id}/file`; research F-5, Q4).

Files go to a temporary folder (`config.UPLOAD_DIR` points at the test's tmp_path, the bucket key unset); no test
calls Google Cloud Storage or any network. The photos are the test's own few bytes behind a real PNG, JPEG or PDF
signature. Cases are found by tag and facts by kind; the counts are the test's own values.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from api import caps, config, db, signals
from tests.api.conftest import as_seat, key, seat
from tests.api.test_evidence import ask, confirm, counted, from_inbox, refused, request_row, signal_row, story, table
from tests.api.test_http import detail, error, multipart, phantom, story as http_story
from tests.api.test_workflow import call

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00 a test shelf photo \xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR a test png"
PDF = b"%PDF-1.4\n% a test delivery note\n%%EOF\n"


@pytest.fixture(autouse=True)
def uploads(tmp_path, monkeypatch):
    """Every stored file goes to this test's own folder; the bucket key is unset (the local branch)."""
    folder = tmp_path / "uploads"
    monkeypatch.setattr(config, "UPLOAD_DIR", str(folder))
    monkeypatch.setattr(config, "UPLOADS_BUCKET", "")
    return folder


def answer_with(run, q, data, counts=None, text="", observed="now") -> dict:
    """The addressed branch answers with a file (bytes) beside its counts and reply."""
    return call(run, signals.answer, seat(q["to_branch"]), q["request_id"], text, counts, observed, data)


def global_uploads() -> int:
    """Today's cross-workspace upload count (the infrastructure cap, caps.global.upload)."""
    with db.tx() as conn:
        row = db.one(conn, "SELECT count FROM usage_counts WHERE scope = :g AND kind = 'upload' AND window_start = :w",
                     g=caps.GLOBAL, w=caps.window_start("day"))
    return row["count"] if row else 0


def own_uploads(run_id: str, *sessions: str) -> int:
    """Upload counts in any scope but the global one this workspace or these sessions could own."""
    with db.tx() as conn:
        return db.one(conn, "SELECT count(*) AS n FROM usage_counts WHERE kind = 'upload' AND scope = ANY(:s)",
                      s=[f"run:{run_id}", run_id, *sessions])["n"]


def no_location(view: dict) -> None:
    """A view never carries where a file is stored (D-20: no bucket URL, no path)."""
    text = json.dumps(view)
    assert "gs://" not in text and "file://" not in text and "asset://" not in text and "file_uri" not in text


def test_photo_with_counts_confirms_and_says_photo_attached(run, uploads):
    """A photo with both counts for every lot answers the request; the confirmed label ends ' · photo attached'. The
    file is stored under <run_id>/<sha256>.<ext> and spends only the cross-workspace upload cap (D-20)."""
    case = story(run)
    q = ask(run, case["case_id"])
    before = global_uploads()
    s = answer_with(run, q, JPEG, counted(q, 2.0))
    assert s["file"] == {"type": "image/jpeg", "size": len(JPEG)}
    no_location(s)
    assert request_row(run, q)["state"] == "ANSWERED" and from_inbox(run, q)["status_kind"] == "ANSWERED"
    assert global_uploads() == before + 1
    assert own_uploads(run["run_id"], "t") == 0  # never a workspace or session allowance
    sha = hashlib.sha256(JPEG).hexdigest()
    row = signal_row(run, s["signal_id"])
    assert (row["file_type"], row["file_sha256"], row["file_size"], row["file_counted"]) == (
        "image/jpeg", sha, len(JPEG), False)
    stored = uploads / run["run_id"] / f"{sha}.jpg"
    assert stored.read_bytes() == JPEG and row["file_uri"].endswith(f"{run['run_id']}/{sha}.jpg")
    out = confirm(run, seat(q["to_branch"]), s["signal_id"], s["revision"])
    assert out["labels"] and all(label.endswith(" · photo attached") for label in out["labels"])
    (fact,) = table(run["run_id"], "reported_facts")
    assert fact["label"].endswith(" · photo attached")


def test_a_count_without_a_photo_says_nothing_about_one(run):
    case = story(run)
    q = ask(run, case["case_id"])
    s = answer_with(run, q, None, counted(q, 1.0))
    assert s["file"] is None
    out = confirm(run, seat(q["to_branch"]), s["signal_id"], s["revision"])
    assert not any("photo" in label for label in out["labels"])


def test_a_photo_only_revision_never_confirms_an_earlier_one(run):
    """Edge VER-04 concurrency: a photo-only revision that lands while an earlier complete one is being confirmed
    makes that confirm refused (the replaced answer is DISMISSED; the current answer with the earlier revision is 409
    stale_version), and the photo-only answer is not confirmable."""
    case = story(run)
    q = ask(run, case["case_id"])
    to = seat(q["to_branch"])
    complete = answer_with(run, q, None, counted(q, 3.0))
    photo = answer_with(run, q, JPEG)
    assert photo["revision"] == complete["revision"] + 1
    e = refused(run, signals.confirm, to, complete["signal_id"], complete["revision"], True)
    assert (e.status, e.code) == (409, "wrong_state")
    e = refused(run, signals.confirm, to, photo["signal_id"], complete["revision"], True)
    assert (e.status, e.code, e.extra["revision"]) == (409, "stale_version", photo["revision"])
    e = refused(run, signals.confirm, to, photo["signal_id"], photo["revision"], True)
    assert (e.status, e.code) == (409, "needs_input")
    assert request_row(run, q)["state"] == "OPEN" and table(run["run_id"], "reported_facts") == []


def http_ask(client) -> dict:
    """The story case's phantom fact asked about by the source seat (the client's start store), then the client
    signed in as the addressed branch: the request."""
    card = http_story(client)
    fact = phantom(detail(client, card["case_id"]))
    r = client.post(f"/api/cases/{card['case_id']}/requests", json={"rec_version": card["rec_version"],
                                                                    "fact_id": fact["fact_id"]}, headers=key())
    assert r.status_code == 200, r.json()
    as_seat(client, fact["branch"])
    return r.json()


def http_answer(client, q, name, data, ctype, text="", counts=None, headers=None):
    files = {**multipart(text, counts or {}), "file": (name, data, ctype)}
    return client.post(f"/api/requests/{q['request_id']}/answer", files=files, headers=headers or key())


def test_download_returns_the_bytes_safely(client):
    """The addressed seat reads the photo back through the API only: the same bytes with the stored type, nosniff,
    private no-store, inline for an image and attachment for a PDF; never a redirect or a bucket URL (D-20)."""
    q = http_ask(client)
    r = http_answer(client, q, "shelf.jpg", JPEG, "image/jpeg")
    assert r.status_code == 200, r.json()
    assert r.json()["file"] == {"type": "image/jpeg", "size": len(JPEG)}
    no_location(r.json())
    got = client.get(f"/api/signals/{r.json()['signal_id']}/file", follow_redirects=False)
    assert got.status_code == 200 and got.content == JPEG
    assert got.headers["content-type"] == "image/jpeg"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["cache-control"] == "private, no-store"
    assert got.headers["content-disposition"].startswith("inline")
    pdf = http_answer(client, q, "note.pdf", PDF, "application/pdf")
    assert pdf.status_code == 200, pdf.json()
    got = client.get(f"/api/signals/{pdf.json()['signal_id']}/file", follow_redirects=False)
    assert got.status_code == 200 and got.content == PDF
    assert got.headers["content-type"] == "application/pdf"
    assert got.headers["content-disposition"].startswith("attachment")
    view = next(x for x in client.get("/api/requests").json() if x["request_id"] == q["request_id"])
    assert view["answer"]["file"] == {"type": "application/pdf", "size": len(PDF)}
    no_location(view)
    nothing = http_answer(client, q, "", b"", "application/octet-stream", text="no photo this time")
    assert nothing.status_code == 200 and nothing.json()["file"] is None  # an empty file part is no file
    assert error(client.get(f"/api/signals/{nothing.json()['signal_id']}/file")) == (404, "not_found")


def test_text_and_type_are_stored_as_given(client):
    """Edge VER-03 encoding: the reply is stored as given in UTF-8 and a NUL is refused; the stored type comes from
    the file's first bytes, never its name or Content-Type."""
    q = http_ask(client)
    reply = "Two crates on the shelf · café ½ box behind"
    r = http_answer(client, q, "photo.pdf", PNG, "application/pdf", text=reply)
    assert r.status_code == 200, r.json()
    assert (r.json()["text"], r.json()["file"]["type"]) == (reply, "image/png")
    got = client.get(f"/api/signals/{r.json()['signal_id']}/file")
    assert got.headers["content-type"] == "image/png" and got.headers["content-disposition"].startswith("inline")
    bad = http_answer(client, q, "photo.jpg", JPEG, "image/jpeg", text="a\x00b")
    assert error(bad) == (400, "bad_request")


# --- refusals and the seat rule (07-07 Task 2) ------------------------------------------------------------------------

def branches() -> list[str]:
    """The stores of the ACTIVE template's reference data."""
    from api import refdata, sandbox
    with db.tx() as conn:
        return list(refdata.get(sandbox.active_template(conn)["dataset_version"]).branches)


def test_cross_workspace_refused(client):
    """Acceptance check 15 (D-20): a file is found by the session's workspace only (another workspace's session gets
    404 for the same signal id, whatever its seat), and inside the workspace only a seat party to it reads it: the
    answering branch, the case's source and HQ do; a manager of an unrelated branch gets 403."""
    from fastapi.testclient import TestClient
    from api.app import app
    from tests.api.conftest import sign_in
    card = http_story(client)
    source = card["source"]
    q = http_ask(client)
    r = http_answer(client, q, "shelf.jpg", JPEG, "image/jpeg")
    assert r.status_code == 200, r.json()
    path = f"/api/signals/{r.json()['signal_id']}/file"
    other = TestClient(app)
    sign_in(other)
    assert error(other.get(path)) == (404, "not_found")
    as_seat(other, q["to_branch"])  # the same seat, another workspace
    assert error(other.get(path)) == (404, "not_found")
    assert client.get(path).content == JPEG  # the answering (addressed) branch
    as_seat(client, source)
    assert client.get(path).content == JPEG  # the linked case's source
    unrelated = next(b for b in branches() if b not in {q["to_branch"], source, card.get("dest")})
    as_seat(client, unrelated)
    refused_read = client.get(path)
    assert refused_read.status_code == 403 and error(refused_read) == (403, "forbidden")
    assert refused_read.json()["message"] == "You can't open this file"
    as_seat(client, None)
    assert client.get(path).content == JPEG  # HQ


def stored_files(folder) -> list:
    return [p for p in folder.rglob("*") if p.is_file()] if folder.exists() else []


def test_a_disguised_file_is_refused(client, uploads):
    """T-07-20, T-07-22: a text file named photo.jpg sent as image/jpeg is 415 and stores nothing; a file part one byte
    over uploads.max_bytes is 413; a body over the upload bound is 400 before any file logic; two file parts are 400;
    every other route keeps the 64 KiB JSON bound. No refusal spends an upload."""
    q = http_ask(client)
    before = global_uploads()
    fake = http_answer(client, q, "photo.jpg", b"only text, named photo.jpg", "image/jpeg")
    assert error(fake) == (415, "bad_file")
    assert fake.json()["message"] == "Only PNG, JPEG or PDF files can be attached"
    big = http_answer(client, q, "big.jpg", JPEG + b"\0" * (config.UPLOAD_MAX_BYTES + 1 - len(JPEG)), "image/jpeg")
    assert error(big) == (413, "too_large") and big.json()["message"] == "Files can be at most 5 MB"
    huge = http_answer(client, q, "huge.jpg", JPEG + b"\0" * (config.UPLOAD_MAX_BYTES + config.UPLOAD_FORM_BYTES),
                       "image/jpeg")
    assert error(huge) == (400, "bad_request") and huge.json()["message"] == "The request body is too large"
    path = f"/api/requests/{q['request_id']}/answer"
    fields = [(k, v) for k, v in multipart("", {}).items()]
    for second in ("file", "photo"):
        two = client.post(path, files=fields + [("file", ("a.jpg", JPEG, "image/jpeg")),
                                                (second, ("b.jpg", JPEG, "image/jpeg"))], headers=key())
        assert error(two) == (400, "bad_request"), second
    padded = client.post(f"/api/requests/{q['request_id']}/cant-check", json={"pad": "x" * config.MAX_BODY_BYTES},
                         headers=key())
    assert error(padded) == (400, "bad_request") and padded.json()["message"] == "The request body is too large"
    assert stored_files(uploads) == [] and global_uploads() == before
    view = next(x for x in client.get("/api/requests").json() if x["request_id"] == q["request_id"])
    assert (view["answer"], view["state"]) == (None, "OPEN")


def test_a_retried_upload_counts_once(client, uploads):
    """T-07-24: the same Idempotency-Key with the same file returns the stored response and counts one upload; the same
    key with another file is 409 idempotency_conflict and counts nothing."""
    q = http_ask(client)
    before = global_uploads()
    headers = key()
    first = http_answer(client, q, "shelf.jpg", JPEG, "image/jpeg", headers=headers)
    again = http_answer(client, q, "shelf.jpg", JPEG, "image/jpeg", headers=headers)
    assert first.status_code == again.status_code == 200 and again.json() == first.json()
    assert global_uploads() == before + 1 and len(stored_files(uploads)) == 1
    other = http_answer(client, q, "shelf.png", PNG, "image/png", headers=headers)
    assert error(other) == (409, "idempotency_conflict")
    assert global_uploads() == before + 1 and len(stored_files(uploads)) == 1
    view = next(x for x in client.get("/api/requests").json() if x["request_id"] == q["request_id"])
    assert view["answer"]["revision"] == first.json()["revision"]


# --- the bucket branch, packaged assets, the storage status (07-07 Task 3) --------------------------------------------

class FakeBlob:
    def __init__(self, store: dict, name: str):
        self.store, self.name = store, name

    def exists(self) -> bool:
        return self.name in self.store

    def upload_from_string(self, data: bytes, content_type: str) -> None:
        self.store[self.name] = (data, content_type)
        self.store.setdefault("_uploads", []).append(self.name)

    def download_as_bytes(self) -> bytes:
        return self.store[self.name][0]


class FakeBucket:
    def __init__(self, store: dict):
        self.store = store

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self.store, name)


class FakeClient:
    """A stand-in for google.cloud.storage.Client: objects kept in a dict, no network."""
    def __init__(self, fail: bool = False):
        self.objects: dict[str, dict] = {}
        self.fail = fail

    def bucket(self, name: str) -> FakeBucket:
        if self.fail:
            raise RuntimeError("the fake bucket is unreachable")
        return FakeBucket(self.objects.setdefault(name, {}))

    def list_blobs(self, bucket: str, max_results: int = 1):
        if self.fail:
            raise RuntimeError("the fake bucket is unreachable")
        return iter(list(self.objects.get(bucket, {}))[:max_results])


BUCKET = "test-uploads"  # the test's own placeholder for the UPLOADS_BUCKET key, never a real bucket


def test_the_bucket_branch_uses_content_names(monkeypatch):
    """With UPLOADS_BUCKET set, save writes <run_id>/<sha256>.<ext> with the sniffed type through the client seam,
    skips a blob that is already there, and read returns its bytes; the real storage library is never imported."""
    import sys
    from api import files
    fake = FakeClient()
    monkeypatch.setattr(config, "UPLOADS_BUCKET", BUCKET)
    monkeypatch.setattr(files, "_client", lambda: fake)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", None)  # any import of the real client fails
    sha = hashlib.sha256(JPEG).hexdigest()
    stored = files.save("run-test", JPEG)
    assert (stored.uri, stored.mime, stored.sha256, stored.size) == (
        f"gs://{BUCKET}/run-test/{sha}.jpg", "image/jpeg", sha, len(JPEG))
    assert fake.objects[BUCKET][f"run-test/{sha}.jpg"] == (JPEG, "image/jpeg")
    again = files.save("run-test", JPEG)
    assert again == stored and fake.objects[BUCKET]["_uploads"] == [f"run-test/{sha}.jpg"]  # skipped: already there
    assert files.read(stored.uri) == JPEG


def test_assets_read_only_named_files():
    """Preloaded material reads packaged files by name from DEMO_SIGNALS_DIR; a name that could leave the folder
    (a separator, '..', an absolute path) is refused (T-07-21)."""
    import pathlib
    from api import files
    from api.errors import ApiError
    uri = files.asset_uri("fixture-photo.jpg")  # the suite's DEMO_SIGNALS_DIR is the fixture folder (07-16)
    assert uri == "asset://fixture-photo.jpg"
    assert files.read(uri) == (pathlib.Path(config.DEMO_SIGNALS_DIR) / "fixture-photo.jpg").read_bytes()
    for bad in ("../api/config.py", "/etc/hosts", "sub/fixture-photo.jpg", "..", ""):
        with pytest.raises(ValueError):
            files.asset_uri(bad)
        with pytest.raises(ApiError) as e:
            files.read(f"asset://{bad}")
        assert (e.value.status, e.value.code) == (404, "not_found")
    with pytest.raises(ApiError):
        files.read("file:///etc/hosts")  # a local path outside UPLOAD_DIR is never read


def test_storage_status(monkeypatch, tmp_path):
    """'ok' for a writable upload folder or a reachable bucket; 'degraded', never an exception, when the bucket's
    client fails or the folder can't be written (readyz's storage key, handed off)."""
    from api import files
    assert files.storage_status() == "ok"  # the fixture's tmp folder
    blocked = tmp_path / "a-file"
    blocked.write_text("not a folder")
    monkeypatch.setattr(config, "UPLOAD_DIR", str(blocked))
    assert files.storage_status() == "degraded"
    monkeypatch.setattr(config, "UPLOADS_BUCKET", BUCKET)
    monkeypatch.setattr(files, "_client", lambda: FakeClient())
    assert files.storage_status() == "ok"
    monkeypatch.setattr(files, "_client", lambda: FakeClient(fail=True))
    assert files.storage_status() == "degraded"

    def no_client():
        raise RuntimeError("no credentials")
    monkeypatch.setattr(files, "_client", no_client)
    assert files.storage_status() == "degraded"
