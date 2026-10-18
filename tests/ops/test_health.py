"""DB-free readiness probe tests (11-CONTEXT D-01, D-13, D-18); fake SDKs, no network."""
from __future__ import annotations

import builtins
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import config, ready
from api.app import app

KEYS = ["ok", "db", "vertex_ai", "cloud_storage", "data_bucket", "uploads_bucket", "model_id", "checked_at"]


@pytest.fixture(autouse=True)
def reachable(monkeypatch):
    monkeypatch.setattr(ready, "db_alive", lambda: True)
    monkeypatch.setattr(config, "GCP", False)


def test_gcp_off_is_disabled_with_zero_calls(monkeypatch):
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert not name.startswith("google"), name
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    r = TestClient(app).get("/api/readyz")
    assert r.status_code == 200
    p = r.json()
    assert list(p) == KEYS
    assert p["ok"] is True and p["db"] == "ok"
    assert [p[k] for k in ("vertex_ai", "cloud_storage", "uploads_bucket")] == ["disabled"] * 3
    assert p["data_bucket"] == "not_checked" and p["model_id"] == config.GEMINI_MODEL
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", p["checked_at"])


def test_ok_body_serializes_the_uptime_substring(monkeypatch):
    from agent import health
    monkeypatch.setattr(config, "GCP", True)
    monkeypatch.setattr(health, "check_ai_and_storage", lambda *args: {
        "vertex_ai": "ok", "uploads_bucket": "ok", "cloud_storage": "ok", "data_bucket": "not_checked",
        "checked_at": "2026-10-18T02:00:00Z"})
    r = TestClient(app).get("/api/readyz")
    assert '\"vertex_ai\":\"ok\",\"cloud_storage\":\"ok\"' in r.text


@pytest.mark.parametrize("status", ["degraded", "ok", "disabled"])
def test_cloud_storage_follows_the_uploads_bucket(monkeypatch, status):
    from agent import health
    monkeypatch.setattr(config, "GCP", True)
    monkeypatch.setattr(health, "check_ai_and_storage", lambda *args: {
        "vertex_ai": "ok", "uploads_bucket": status, "cloud_storage": "ok", "data_bucket": "not_checked",
        "checked_at": "2026-10-18T02:00:00Z"})
    p = TestClient(app).get("/api/readyz").json()
    assert p["cloud_storage"] == status and p["data_bucket"] == "not_checked"


def test_missing_health_module_reads_degraded(monkeypatch, capsys):
    monkeypatch.setattr(config, "GCP", True)
    original = ready.importlib.import_module
    def missing(name, *args, **kwargs):
        if name == "agent.health":
            raise ModuleNotFoundError(name)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(ready.importlib, "import_module", missing)
    r = TestClient(app).get("/api/readyz")
    assert r.status_code == 200
    assert [r.json()[k] for k in ("vertex_ai", "cloud_storage", "uploads_bucket")] == ["degraded"] * 3
    assert r.json()["data_bucket"] == "not_checked"
    line = json.loads(capsys.readouterr().err)
    assert line["severity"] == "ERROR" and line["component"] == "readiness"


def test_web_database_guard_is_strict():
    t = (Path(__file__).resolve().parents[1] / "web/conftest.py").read_text()
    pattern = re.search(r're\.fullmatch\(r?"([^"]+)"\s*,\s*name\)', t)[1]
    for name in ("shelfshift_web_test", "shelfshift_freeze_web_test"):
        assert re.fullmatch(pattern, name)
    for name in ("shelfshift_freeze", "shelfshift_test", "shelfshift"):
        assert not re.fullmatch(pattern, name)

# Task 2: GCP-on paths run against fake modules only.
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor


@pytest.fixture
def probes(monkeypatch):
    from agent import health
    pool = ThreadPoolExecutor(max_workers=2)
    releases = []
    monkeypatch.setattr(health, "_pool", pool, raising=False)
    monkeypatch.setattr(health, "_cache", {"at": -health.TTL_S, "payload": None}, raising=False)
    monkeypatch.setattr(health, "_in_flight", {}, raising=False)
    monkeypatch.setattr(health, "_lock", threading.Lock(), raising=False)
    yield health, releases
    for event in releases:
        event.set()
    pool.shutdown(wait=True)


def fake_sdks(monkeypatch, vertex=lambda: None, listing=lambda: []):
    calls = {"clients": [], "models": [], "listings": [], "closes": []}
    google = types.ModuleType("google")
    google.__path__ = []
    genai = types.ModuleType("google.genai")
    genai.__path__ = []
    genai_types = types.ModuleType("google.genai.types")
    cloud = types.ModuleType("google.cloud")
    cloud.__path__ = []
    storage = types.ModuleType("google.cloud.storage")
    class HttpOptions:
        def __init__(self, *, timeout):
            self.timeout = timeout
    class GenaiClient:
        def __init__(self, **kwargs):
            calls["clients"].append(kwargs)
            self.models = self
        def get(self, **kwargs):
            calls["models"].append(kwargs)
            return vertex()
        def close(self):
            calls["closes"].append("vertex")
    class StorageClient:
        def list_blobs(self, name, **kwargs):
            calls["listings"].append((name, kwargs))
            return listing()
        def close(self):
            calls["closes"].append("storage")
    genai_types.HttpOptions = HttpOptions
    genai.Client = GenaiClient
    genai.types = genai_types
    storage.Client = StorageClient
    cloud.storage = storage
    google.genai = genai
    google.cloud = cloud
    for module in (google, genai, genai_types, cloud, storage):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return calls


def test_probes_run_concurrently_under_one_deadline(probes, monkeypatch):
    health, _ = probes
    calls = fake_sdks(monkeypatch, vertex=lambda: time.sleep(1), listing=lambda: (time.sleep(1) or []))
    started = time.monotonic()
    p = health.check_ai_and_storage(True, "uploads")
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert [p[k] for k in ("vertex_ai", "cloud_storage", "uploads_bucket")] == ["ok"] * 3
    assert p["data_bucket"] == "not_checked"
    assert len(calls["models"]) == len(calls["listings"]) == 1


def test_slow_probe_is_degraded_by_the_deadline(probes, monkeypatch, capsys):
    health, releases = probes
    release = threading.Event()
    releases.append(release)
    fake_sdks(monkeypatch, vertex=lambda: release.wait(10))
    started = time.monotonic()
    p = health.check_ai_and_storage(True, "uploads")
    elapsed = time.monotonic() - started
    assert elapsed < 5.4
    assert p["vertex_ai"] == "degraded" and p["uploads_bucket"] == "ok"
    line = json.loads(capsys.readouterr().err)
    assert line["severity"] == "ERROR" and line["component"] == "vertex_ai"
    assert line["event"] == "probe_timeout"


def test_no_overlapping_workers(probes, monkeypatch, capsys):
    health, releases = probes
    release = threading.Event()
    releases.append(release)
    calls = fake_sdks(monkeypatch, vertex=lambda: release.wait(10))
    p = health.check_ai_and_storage(True, "uploads")
    assert p["vertex_ai"] == "degraded"
    health._cache["at"] = -health.TTL_S
    p = health.check_ai_and_storage(True, "uploads")
    assert p["vertex_ai"] == "degraded" and len(calls["models"]) == 1
    assert len(calls["listings"]) == 2
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [line["event"] for line in lines] == ["probe_timeout", "probe_busy"]


def test_cache_and_single_flight(probes, monkeypatch):
    health, _ = probes
    now = [0.0]
    monkeypatch.setattr(health, "time", types.SimpleNamespace(monotonic=lambda: now[0]), raising=False)
    calls = fake_sdks(monkeypatch, vertex=lambda: time.sleep(.05))
    first = health.check_ai_and_storage(True, "uploads")
    now[0] = 59.9
    assert health.check_ai_and_storage(True, "uploads") == first
    assert len(calls["models"]) == 1
    now[0] = 60.0
    health.check_ai_and_storage(True, "uploads")
    assert len(calls["models"]) == 2
    now[0] = 120.0
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: health.check_ai_and_storage(True, "uploads"), range(2)))
    assert results[0] == results[1]
    assert len(calls["models"]) == len(calls["listings"]) == 3
    # A caller cannot mutate the shared cached payload.
    results[0]["vertex_ai"] = "degraded"
    assert health.check_ai_and_storage(True, "uploads")["vertex_ai"] == "ok"


def test_single_attempt_arguments(probes, monkeypatch):
    health, _ = probes
    monkeypatch.setattr(config, "VERTEX_LOCATION", "test-location")
    monkeypatch.setattr(config, "GEMINI_MODEL", "test-model")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("PROJECT", "must-not-use")
    calls = fake_sdks(monkeypatch)
    health.check_ai_and_storage(True, "configured-uploads")
    assert calls["listings"] == [("configured-uploads", {"max_results": 1, "timeout": 3.5, "retry": None})]
    assert len(calls["clients"]) == 1
    kwargs = calls["clients"][0]
    assert {k: v for k, v in kwargs.items() if k != "http_options"} == {
        "vertexai": True, "location": "test-location", "project": "test-project"}
    assert kwargs["http_options"].timeout == 3500
    assert calls["models"] == [{"model": "test-model"}]
    assert sorted(calls["closes"]) == ["storage", "vertex"]


def test_project_is_omitted_without_google_cloud_project(probes, monkeypatch):
    health, _ = probes
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("PROJECT", "must-not-use")
    calls = fake_sdks(monkeypatch)
    health.check_ai_and_storage(True, "uploads")
    assert "project" not in calls["clients"][0]


def test_empty_bucket_is_ok_and_empty_name_is_disabled(probes, monkeypatch):
    health, _ = probes
    calls = fake_sdks(monkeypatch, listing=lambda: iter(()))
    assert health.check_ai_and_storage(True, "uploads")["uploads_bucket"] == "ok"
    health._cache["at"] = -health.TTL_S
    p = health.check_ai_and_storage(True, "")
    assert p["uploads_bucket"] == p["cloud_storage"] == "disabled"
    assert len(calls["listings"]) == 1


def test_missing_sdk_is_disabled_and_forbidden_is_degraded(probes, monkeypatch, capsys):
    health, _ = probes
    secret_bucket = "private-uploads-marker"
    secret_error = "private-exception-marker"
    def forbidden():
        raise PermissionError(secret_error)
    fake_sdks(monkeypatch, listing=forbidden)
    original = builtins.__import__
    def missing(name, *args, **kwargs):
        if name == "google" and "genai" in kwargs.get("fromlist", args[2] if len(args) > 2 else ()):
            raise ImportError("missing")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", missing)
    p = health.check_ai_and_storage(True, secret_bucket)
    assert p["vertex_ai"] == "disabled"
    assert p["uploads_bucket"] == p["cloud_storage"] == "degraded"
    output = capsys.readouterr().err
    assert secret_bucket not in output + json.dumps(p)
    assert secret_error not in output + json.dumps(p)
    assert json.loads(output)["component"] == "cloud_storage"


def test_data_bucket_is_never_probed(probes, monkeypatch, capsys):
    health, _ = probes
    data_marker = "data-bucket-must-never-be-read"
    uploads_marker = "configured-uploads-marker"
    monkeypatch.setattr(config, "GCP", True)
    monkeypatch.setattr(config, "UPLOADS_BUCKET", uploads_marker)
    monkeypatch.setenv("DATA_BUCKET", data_marker)
    calls = fake_sdks(monkeypatch)
    response = TestClient(app).get("/api/readyz")
    assert response.status_code == 200 and response.json()["data_bucket"] == "not_checked"
    assert len(calls["listings"]) == 1 and calls["listings"][0][0] == uploads_marker
    assert data_marker not in str(calls) + response.text + capsys.readouterr().err


def test_dependency_log_uses_fixed_fields_only(capsys):
    ready.log_dependency_error("uploads", "upload_failed")
    line = json.loads(capsys.readouterr().err)
    assert line["severity"] == "ERROR" and line["component"] == "uploads"
    assert set(line) == {"severity", "component", "event", "message"}
