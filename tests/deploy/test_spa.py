"""The SPA route (OPS-03): the built web app is served by the last route, and no request path reads a file outside
the build. DB-free: TestClient is used without a `with` block (no lifespan, no worker, no database).

Traversal vectors go through `raw()`, which hands the ASGI app the request target exactly as uvicorn's h11 protocol
does (`path = unquote(raw_path)`, no dot-segment removal); an HTTP client such as httpx would normalise "/../" away
before the app ever saw it. Every vector compares the body bytes with index.html's, not only the status code.
"""
from __future__ import annotations

import asyncio
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote

os.environ.setdefault("DEMO_PASSCODE", secrets.token_urlsafe(16))  # before importing api: the app refuses a blank one

import pytest  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.routing import Route  # noqa: E402

from api import config  # noqa: E402
from api.app import app, spa  # noqa: E402

INDEX = b'<!doctype html><html><body><div id="root"></div><!-- index-marker --></body></html>'
APP_JS = b"console.log('app-js-marker');"
SECRET = b"SECRET-OUTSIDE-THE-BUILD"
OLD = b"OLD-DIST-SIBLING"
OUTSIDE_INDEX = b"OUTSIDE-INDEX-MARKER"
# The merged release base (build/deploy + the 06.1 store app + Phase 11's freeze): api.app's 22 private routes
# (tests/api/test_http.py counts them) + healthz, readyz, session, the 15 of the 06.1 route modules (api/routes_*.py)
# and Phase 11's three (/api/about, /api/reports/workspace, /api/reports/benchmark) = 43; then Phase 7's 17 in
# api.app (requests, holds, reports, file, select, review, cancel), Phase 9's 6 (api/routes_assistant.py)
# and 07-20's preview before a report is filed (POST /api/signals/preview)
API_ROUTES_AT_PHASE6_BASE = 67


@pytest.fixture
def build(tmp_path, monkeypatch):
    """tmp/web/dist with index.html and assets/app.js; the secret beside the build and one level above it; a sibling
    dist-old whose name begins with the build's name; a symlink inside the build pointing at the secret."""
    web = tmp_path / "web"
    dist = web / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_bytes(INDEX)
    (dist / "assets" / "app.js").write_bytes(APP_JS)
    (web / "secret.txt").write_bytes(SECRET)
    (tmp_path / "secret.txt").write_bytes(SECRET)
    (web / "dist-old").mkdir()
    (web / "dist-old" / "x.txt").write_bytes(OLD)
    (dist / "assets" / "leak.js").symlink_to(web / "secret.txt")
    monkeypatch.setattr(config, "WEB_DIST", str(dist))
    return dist


def client() -> TestClient:
    return TestClient(app)  # no `with`: the lifespan (worker, passcode check) never runs


def raw(method: str, target: str) -> tuple[int, dict, bytes]:
    """(status, headers, body) for a request target sent verbatim, decoded the way uvicorn decodes it."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
        "path": unquote(target), "raw_path": target.encode("ascii"), "root_path": "", "query_string": b"",
        "headers": [(b"host", b"testserver")], "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], headers, body


def error(r) -> tuple[int, str]:
    return r.status_code, r.json().get("error")


# --- what the build serves ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/index.html", "/assets/", "/board", "/cases/SUS-19"])
def test_the_index_is_served_with_no_cache(build, path):
    r = client().get(path)
    assert r.status_code == 200, path
    assert r.content == INDEX, path
    assert r.headers["cache-control"] == "no-cache", path


def test_a_built_asset_is_served_as_itself(build):
    r = client().get("/assets/app.js")
    assert r.status_code == 200 and r.content == APP_JS
    assert r.headers.get("cache-control") != "no-cache"


def test_head_on_the_root_answers_200(build):
    r = client().head("/")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_concurrent_gets_each_receive_a_complete_file(build):
    c = client()
    paths = ["/", "/assets/app.js"] * 10
    with ThreadPoolExecutor(max_workers=8) as pool:
        bodies = list(pool.map(lambda p: (p, c.get(p).content), paths))
    for path, body in bodies:
        assert body == (INDEX if path == "/" else APP_JS), path


# --- traversal vectors: the body is index.html's bytes, never the outside file's ------------------------------------

@pytest.mark.parametrize("target", [
    "/../secret.txt",               # a plain dot-dot
    "/..%2fsecret.txt",             # an encoded slash
    "/%2e%2e/%2e%2e/secret.txt",    # encoded dots, two levels up
    "/assets/../../secret.txt",     # dot-dot after a real directory
    "//etc/passwd",                 # a doubled leading slash (an absolute path once joined)
    "/assets/%00x",                 # an embedded NUL
    "/assets/leak.js",              # a symlink inside the build pointing outside
    "/../dist-old/x.txt",           # a sibling whose name begins with the build's name (no string prefix check)
])
def test_no_vector_reads_a_file_outside_the_build(build, target):
    status, headers, body = raw("GET", target)
    assert status == 200, target
    assert body == INDEX, (target, body[:80])
    assert SECRET not in body and OLD not in body and b"root:" not in body, target
    assert headers["cache-control"] == "no-cache", target


def test_the_vectors_really_point_outside(build):
    """The vectors are meaningful: each named outside file exists where the vector lands."""
    dist = build
    assert (dist / ".." / "secret.txt").resolve().read_bytes() == SECRET
    assert (dist / ".." / ".." / "secret.txt").resolve().read_bytes() == SECRET
    assert (dist / "assets" / "leak.js").resolve().read_bytes() == SECRET
    assert str((dist / ".." / "dist-old" / "x.txt").resolve()).startswith(str(dist.resolve()))  # a prefix match


# --- an index.html that escapes the build counts as no build (06-REVIEWS R2-12) ------------------------------------

@pytest.mark.parametrize("path", ["/", "/index.html", "/some/route"])
def test_an_index_symlinked_outside_the_build_is_no_build(build, tmp_path, path):
    outside = tmp_path / "outside.html"
    outside.write_bytes(OUTSIDE_INDEX)
    (build / "index.html").unlink()
    (build / "index.html").symlink_to(outside)
    r = client().get(path)
    assert error(r) == (404, "not_found"), path
    assert OUTSIDE_INDEX not in r.content, path
    status, _, body = raw("GET", path)
    assert status == 404 and OUTSIDE_INDEX not in body, path


def test_an_index_symlinked_inside_the_build_is_served(build):
    inner = b'<!doctype html><div id="root"></div><!-- inner-index -->'
    (build / "real.html").write_bytes(inner)
    (build / "index.html").unlink()
    (build / "index.html").symlink_to(build / "real.html")
    for path in ("/", "/index.html", "/some/route"):
        r = client().get(path)
        assert r.status_code == 200 and r.content == inner, path
        assert r.headers["cache-control"] == "no-cache", path


def test_an_index_symlink_loop_is_no_build(build):
    (build / "index.html").unlink()
    (build / "index.html").symlink_to(build / "index.html")
    assert error(client().get("/")) == (404, "not_found")


def test_without_a_build_the_route_answers_not_found(build):
    (build / "index.html").unlink()
    for path in ("/", "/index.html", "/board"):
        assert error(client().get(path)) == (404, "not_found"), path


def test_a_missing_build_directory_answers_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WEB_DIST", str(tmp_path / "no-such-dist"))
    assert error(client().get("/")) == (404, "not_found")


# --- /api paths and other methods keep the 404 envelope -------------------------------------------------------------

@pytest.mark.parametrize("method, path", [
    ("GET", "/api/nope"), ("GET", "/api"), ("GET", "/api/"), ("POST", "/api/nope"), ("POST", "/anything"),
    ("PUT", "/"), ("DELETE", "/index.html"),
])
def test_api_paths_and_other_methods_answer_not_found(build, method, path):
    r = client().request(method, path)
    assert error(r) == (404, "not_found"), (method, path)
    assert r.json()["message"] == "No such route"


def test_public_api_routes_still_answer_ahead_of_the_spa(build):
    assert client().get("/api/healthz").json() == {"ok": True}


# --- route order ----------------------------------------------------------------------------------------------------

def test_the_spa_route_is_last_and_a_plain_starlette_route():
    last = app.routes[-1]
    assert type(last) is Route and not isinstance(last, APIRoute)
    assert last.path == "/{path:path}" and last.endpoint is spa
    assert last.methods == {"GET", "HEAD"}


def test_every_api_route_comes_before_the_spa_route():
    spa_at = next(i for i, r in enumerate(app.routes) if getattr(r, "endpoint", None) is spa)
    api_at = [i for i, r in enumerate(app.routes) if isinstance(r, APIRoute)]
    assert api_at and max(api_at) < spa_at == len(app.routes) - 1
    assert all(r.path.startswith("/api/") for r in app.routes if isinstance(r, APIRoute))


def test_the_spa_route_adds_no_api_route():
    assert sum(isinstance(r, APIRoute) for r in app.routes) == API_ROUTES_AT_PHASE6_BASE
