"""The FastAPI app: the demo-path API under /api (spec §10; 04-API-CONTRACT; 04-CONTEXT D-07).

    uvicorn api.app:app --port 8080 --no-proxy-headers

Only the demo-path routes exist (D-07): field signals, uploads, sample reports, the secondary pages, metrics and the
paused page arrive whole with Phases 7, 8 and 11, and any /api path no route serves is 404 not_found.

- **Web app.** The built SPA in `config.WEB_DIST` (web/dist) is served by the last route, registered after every /api
  route (OPS-03). It returns a file only when its resolved path is inside the resolved build (`resolve()` +
  `is_relative_to`, never a string prefix), index.html included; every other path gets index.html with
  `Cache-Control: no-cache`. Without a usable build (API-only runs, tests) it answers 404 not_found.

- **Session.** `POST /api/session` checks the passcode (`secrets.compare_digest` on both sides trimmed, the passcode
  from the environment, never a default in code; a blank one opens nothing and the app refuses to start) and sets
  the `ss_session` cookie (httponly, SameSite=Lax, secure on https and whenever K_SERVICE is set, D-24). Wrong
  passcodes are counted per client address (`client_address`). Every other route except healthz and readyz needs
  an unlocked session, checked by the `current_session` dependency on each private route, not by an HTTP middleware
  (research A15, so Phase 9's streaming disconnects keep working). A route acts only on the run_id and the seat
  stored on that session row, never on one sent by the client (FLOW-02, FLOW-10).
- **Writes** (`_write`) need an `Idempotency-Key`. One transaction inserts the key row first (ON CONFLICT DO NOTHING
  RETURNING): a repeat with the same body returns the stored response, another body is 409 idempotency_conflict, and
  a simultaneous duplicate waits on the key row and then returns the stored response. Then the run is locked FOR
  SHARE (advance-day, settings and count confirm FOR UPDATE) and checked ACTIVE, the service runs, and its response
  is stored. A failed write rolls its key back with everything else. The body hash is sha256 of the JSON body with
  sorted keys. Sign-out and reset scope their key to (key, session, endpoint) with run_id '' (research Pitfall
  6, A13).
- **Reads** (`_read`) lock the run FOR SHARE and read stored versions; no GET calls the engine worker (OPS-06).
- **Errors** are the contract's envelope `{error, message, ...}`: ApiError as raised; an engine InputError,
  AdjustmentError or HorizonError through `errors.engine_error`; a database pool timeout as 503 db_busy; a malformed
  body, one nested too deep, or one larger than the `http.max_body_bytes` setting as 400 bad_request (never a
  framework 422 or a 500); an unknown path as 404 not_found. No traceback reaches a client.
- **Lifespan.** The warm engine worker starts before the first request when ENGINE_WORKER is on, and stops on
  shutdown. There is no compression middleware: it would buffer Phase 9's streams (CLAUDE.md stack notes).
- **Route modules** (06.1). `load_route_modules`, called once at the end of this module, imports every
  `api/routes_*.py` once; such a module registers its routes on `app` with this module's wrappers.
- **Accounts** (06.1 D-02, D-47, D-64, D-68; api/accounts.py). The session's identity is its account: a store code
  (the store passcode) or HQ (its own credential, HQ_PASSCODE). Sign-in rotates the session id; sign-out clears the
  account and keeps the workspace; a session without an account is 401 locked. The app refuses to start without a
  sound HQ credential. The seat (role, branch) is derived from the account and read only from the session row.
"""
from __future__ import annotations

import hashlib
import importlib
import ipaddress
import json
import logging
import pathlib
import pkgutil
import re
from contextlib import asynccontextmanager
from datetime import date, datetime

import sqlalchemy as sa
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.responses import Response  # the file read's bytes (07-07)
from starlette.exceptions import HTTPException as StarletteHTTPException

from engine.adjust import AdjustmentError, InputError
from engine.simulate import HorizonError
from agent import config as assistant_config

from . import (accounts, advance, caps, cases, config, db, flags, ready, refdata, sandbox, settings, signals, views, workflow,
               worker)
from . import files  # uploads and the file read (07-CONTEXT D-20)
from . import lotholds  # lot holds: place and release (07-CONTEXT D-11, D-13)
from . import reviewflags  # Reviewed · keep plan (07-CONTEXT D-17)
from . import selection  # Select another plan (07-CONTEXT D-23)
from . import closing  # Cancel approved plan (07-CONTEXT D-23, D-25)
from . import reports  # field reports filed by hand (07-CONTEXT D-14, D-15, D-20)
from . import impact  # the impact preview of a report (07-CONTEXT D-16)
from .errors import DB_BUSY, ApiError, engine_error

COOKIE = "ss_session"
COOKIE_MAX_AGE_S = 60 * 60 * 24 * 30  # 30 days, as the plan (infrastructure, not a retailer value)
KEY_MAX = 200  # characters: an Idempotency-Key is a client UUID; a longer one could not be indexed
INT_MAX = 2**31 - 1  # a whole number the database's int columns hold
LOCKED = "Sign in to continue."  # (proposed) 06.1 contract, GET /api/session row
PREPARING = "ShelfShift is opening your workspace. Try again in a minute."  # (proposed) 06.1-05; contract change-log
NOT_HERE = "This is not available in this workspace."  # (proposed) 06.1 contract: a demo control outside demo mode
log = logging.getLogger("shelfshift.api")
TOO_LARGE = "The request body is too large"
NO_PASSCODE = "DEMO_PASSCODE is not set; refusing to start"  # names the variable, never a value (CR-01)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """A warm engine worker before the first request (OPS-06); the pool is shut down when the app stops. Without a
    passcode the app refuses to start: the gate fails closed, never open (CR-01). Without a sound HQ credential it
    refuses too (06.1 D-64, D-68; accounts.hq_config_problem)."""
    if not config.DEMO_PASSCODE.strip():
        raise RuntimeError(NO_PASSCODE)
    if (hq_problem := accounts.hq_config_problem()) is not None:  # names HQ_PASSCODE and the rule, never a value
        raise RuntimeError(hq_problem)
    if (problem := ready.startup_problem()) is not None:
        raise RuntimeError(problem)
    assistant_config.load()  # required finite budgets; no model/client construction or generation at setup
    if config.ENGINE_WORKER:
        worker.start()
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(title="ShelfShift", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


# --- error envelope -------------------------------------------------------------------------------------------------

def _envelope(exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": exc.code, "message": exc.message, **exc.extra}, status_code=exc.status)


@app.exception_handler(ApiError)
async def api_error(_: Request, exc: ApiError):
    return _envelope(exc)


@app.exception_handler(InputError)
@app.exception_handler(AdjustmentError)
@app.exception_handler(HorizonError)
async def engine_failure(_: Request, exc: Exception):
    return _envelope(engine_error(exc))


@app.exception_handler(sa.exc.TimeoutError)
async def db_busy(_: Request, __: Exception):
    return _envelope(ApiError(503, "db_busy", DB_BUSY))


@app.exception_handler(RequestValidationError)
async def bad_request(_: Request, __: Exception):
    return _envelope(ApiError(400, "bad_request", "The request is malformed"))


@app.exception_handler(StarletteHTTPException)
async def no_route(_: Request, exc: StarletteHTTPException):
    if exc.status_code in (404, 405):  # every route not in the contract's tables answers 404 not_found
        return _envelope(ApiError(404, "not_found", "No such route"))
    return _envelope(ApiError(400, "bad_request", "The request is malformed"))


@app.exception_handler(ready.Paused)
async def unavailable(_: Request, __: ready.Paused):
    return JSONResponse(ready.paused_body(), status_code=503)


ready.install(app)


# --- session, body and the write and read wrappers ------------------------------------------------------------------

def workspace_session(request: Request) -> dict:
    """The caller's session row; 401 locked without a cookie, without a session row or without a sandbox."""
    sid = request.cookies.get(COOKIE)
    s = None
    if sid:
        with db.tx() as conn:
            s = db.one(conn, "SELECT * FROM sessions WHERE session_id = :s", s=sid)
    if s is None or not s["run_id"]:
        raise ApiError(401, "locked", LOCKED)
    return s


def current_session(request: Request) -> dict:
    """A signed-in session row: `workspace_session`'s, and 401 locked without an account too (06.1 D-02: a
    signed-out browser keeps its workspace but reaches nothing)."""
    s = workspace_session(request)
    if not s.get("account"):
        raise ApiError(401, "locked", LOCKED)
    return s


async def bounded_body(request: Request, limit: int | None = None) -> bytes:
    """The raw body, read up to `config.MAX_BODY_BYTES` (a setting) whether or not the client sent a Content-Length;
    a larger one is 400 bad_request before anything else reads it (WR-02). The body is kept on the request, so a
    form parser reading it afterwards sees the same bytes. An upload route passes its own `limit` (research F-5)."""
    limit = config.MAX_BODY_BYTES if limit is None else limit
    size = request.headers.get("content-length")
    if size is not None and (not size.strip().isdigit() or int(size) > limit):
        raise ApiError(400, "bad_request", TOO_LARGE)
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > limit:
            raise ApiError(400, "bad_request", TOO_LARGE)
    request._body = bytes(raw)  # Starlette's own cache: request.form() and request.body() read it from here
    return request._body


async def json_body(request: Request) -> dict:
    """The JSON object body ({} when empty); anything else is 400 bad_request, never a framework 422. Nesting too
    deep for the parser (RecursionError) is a malformed body too, never a 500 (WR-02)."""
    raw = await bounded_body(request)
    if not raw.strip():
        return {}
    if b"\x00" in raw or b"\\u0000" in raw:  # Postgres text and jsonb cannot hold a NUL
        raise ApiError(400, "bad_request", "The request body holds a NUL character")
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise ApiError(400, "bad_request", "The request body must be JSON") from None
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "The request body must be a JSON object")
    return body


def _text(body: dict, name: str, required: bool = True) -> str | None:
    value = body.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ApiError(400, "bad_request", f"{name} is required as text")
    return value


def _whole(body: dict, name: str) -> int:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > INT_MAX:
        raise ApiError(400, "bad_request", f"{name} is required as a whole number")
    return value


def _actor(s: dict) -> dict:
    """The acting account and its seat, from the session row only, never from a request body (FLOW-10; 06.1 D-47)."""
    return {"account": s.get("account"), "role": s["role"], "branch": s["branch"], "session_id": s["session_id"]}


def _plain(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON")


def _jsonable(value):
    """The response as JSON stores and returns it, so a repeat with the key is exactly the first response."""
    return json.loads(json.dumps(value, default=_plain))


def body_hash(body: dict) -> str:
    """sha256 of the JSON body with sorted keys: the same fields in another key order are the same request."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _request_key(request: Request) -> str:
    """The request's Idempotency-Key; 400 idempotency_key when it is missing, blank or too long."""
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key or len(key) > KEY_MAX:
        raise ApiError(400, "idempotency_key", "Idempotency-Key header required")
    return key


def _write(request: Request, s: dict, endpoint: str, body: dict, fn, mode: str = "SHARE", run_scoped: bool = True):
    """One transaction: the idempotency key row first, then the run lock, then the change, then the stored response
    (spec §8 Idempotency, Locking rule). `run_scoped=False` (sign-out, reset) keys on (key, session, endpoint)
    with run_id '' and takes no run lock."""
    key = _request_key(request)
    if len(endpoint) > KEY_MAX:
        raise ApiError(404, "not_found", "No such route")
    digest = body_hash(body)
    scope = {"k": key, "s": s["session_id"], "r": s["run_id"] if run_scoped else "", "e": endpoint}

    def work(conn):
        stored = db.one(conn, "INSERT INTO idempotency_keys (key, session_id, run_id, endpoint, body_hash) "
                              "VALUES (:k, :s, :r, :e, :h) ON CONFLICT DO NOTHING RETURNING key", h=digest, **scope)
        if stored is None:  # a repeat, or a duplicate that waited on the key row until the first one committed
            prev = db.one(conn, "SELECT body_hash, response FROM idempotency_keys WHERE key = :k AND session_id = :s "
                                "AND run_id = :r AND endpoint = :e", **scope)
            if prev["body_hash"] != digest:
                raise ApiError(409, "idempotency_conflict", "This Idempotency-Key was used with a different request")
            return prev["response"]
        run = db.lock_run(conn, s["run_id"], mode=mode) if run_scoped else None
        out = _jsonable(fn(conn, run, _actor(s), s))
        db.run(conn, "UPDATE idempotency_keys SET status = 200, response = CAST(:p AS jsonb) WHERE key = :k "
                     "AND session_id = :s AND run_id = :r AND endpoint = :e", p=db.js(out), **scope)
        return out
    return db.in_tx(work)


def _read(s: dict, fn):
    """A read in one transaction after the run lock FOR SHARE (the run must be ACTIVE). Reads apply hold expiry, as
    spec §8 asks of any read that touches a case, but never call the engine worker (OPS-06)."""
    def work(conn):
        run = db.lock_run(conn, s["run_id"])
        return fn(conn, run, _actor(s), s)
    return db.in_tx(work)


async def answer_form(request: Request) -> dict:
    """The answer's multipart fields: `text` (a string, may be empty), `counts` (a string holding a JSON object, or
    blank), `dates` and `checked_lot` (an expiry part's printed dates as a JSON object, or blank, and "true" for the
    tick "I checked the specified lot"; 07-CONTEXT D-11), `observed` ("When did you check?": "now" or an ISO-8601
    time with its offset; absent is None, which the service refuses as 400 check_time, never a default; 07-CONTEXT
    D-22) and one optional `file` part (PNG,
    JPEG or PDF by content; D-20). The body is read up to `uploads.max_bytes` + `uploads.form_bytes` (research F-5),
    the fields beside the file up to `uploads.form_bytes`; a file part over `uploads.max_bytes` is 413 too_large, one
    of another type 415 bad_file, a second file part (or a file under another name) 400. An empty file part is no
    file. The returned `file` (bytes) travels beside the fields; `file_sha256` and `file_size` go into the body hash
    with them, so a retried key with another file is 409 idempotency_conflict. Anything malformed is 400."""
    ctype = request.headers.get("content-type", "")
    if not ctype.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        raise ApiError(400, "bad_request", "Send the answer as multipart/form-data")
    await bounded_body(request, config.UPLOAD_MAX_BYTES + config.UPLOAD_FORM_BYTES)
    form = await request.form(max_files=1, max_fields=20)
    upload = form.get("file")
    if isinstance(upload, str):  # a field, not a file part: blank is no file (an empty file input), else 400
        if upload:
            raise ApiError(400, "bad_request", "Attach the file as a file part")
        upload = None
    if any(not isinstance(v, str) for k, v in form.multi_items() if k != "file") or len(form.getlist("file")) > 1:
        raise ApiError(400, "bad_request", "Attach one file, as the file part")
    if sum(len(v.encode("utf-8")) for _, v in form.multi_items() if isinstance(v, str)) > config.UPLOAD_FORM_BYTES:
        raise ApiError(400, "bad_request", TOO_LARGE)
    data = await upload.read(config.UPLOAD_MAX_BYTES + 1) if upload is not None else b""
    if len(data) > config.UPLOAD_MAX_BYTES:
        raise files.too_large()
    if data:
        files.sniff(data)  # 415 before anything is written (the service stores what it sniffs again)
    text, raw, observed = form.get("text", ""), form.get("counts") or "", form.get("observed")
    raw_dates, tick = form.get("dates") or "", form.get("checked_lot") or ""
    if any("\x00" in v for v in (text, raw, observed or "", raw_dates, tick)):
        raise ApiError(400, "bad_request", "The answer holds a NUL character")
    counts = None
    if raw.strip():
        try:
            counts = json.loads(raw)
        except (ValueError, RecursionError):
            raise ApiError(400, "bad_request", "counts must be a JSON object of lot counts") from None
        if not isinstance(counts, dict):
            raise ApiError(400, "bad_request", "counts must be a JSON object of lot counts")
    dates = None  # an expiry part's printed dates: {lot_id: "YYYY-MM-DD" | "MIXED"} (07-CONTEXT D-11)
    if raw_dates.strip():
        try:
            dates = json.loads(raw_dates)
        except (ValueError, RecursionError):
            raise ApiError(400, "bad_request", "dates must be a JSON object of printed dates") from None
        if not isinstance(dates, dict):
            raise ApiError(400, "bad_request", "dates must be a JSON object of printed dates")
    if tick not in ("", "true", "false"):
        raise ApiError(400, "bad_request", "checked_lot is \"true\" or left out")
    return {"text": text, "counts": counts, "observed": observed, "dates": dates, "checked_lot": tick == "true",
            "file": data or None, "file_sha256": hashlib.sha256(data).hexdigest() if data else None,
            "file_size": len(data) or None}


def _prune() -> None:
    """One bounded batch of data no live session can reach (IN-01, `sandbox.prune`), in its own short transaction
    after the visitor's write committed. A database error here is logged and never fails the visitor's request."""
    try:
        db.in_tx(sandbox.prune)
    except sa.exc.SQLAlchemyError as exc:
        log.warning("prune skipped: %s", type(exc).__name__)


Session = Depends(current_session)
Body = Depends(json_body)


# --- public ---------------------------------------------------------------------------------------------------------

@app.get("/api/healthz")
def healthz():
    return {"ok": True}  # liveness only: no dependency calls


@app.get("/api/readyz")
def readyz():
    """A readiness report, not the error envelope: 503 only when Postgres is unreachable."""
    return ready.readyz()


def client_address(request: Request) -> str | None:
    """The address the wrong-passcode counter counts (06-CONTEXT D-24). On Cloud Run (`config.ON_CLOUD_RUN`, from
    K_SERVICE) it is the rightmost X-Forwarded-For entry: Google's front end appends the address it saw, so a visitor
    cannot write that entry, and every entry to its left is client-written and ignored. Several header lines are read
    in order as one list. A missing header, or a last entry that is empty or not an IP address, falls back to the
    peer. Locally it is the TCP peer, because every uvicorn launcher runs with proxy headers off (06-01). Visitors
    behind one shared NAT or VPN exit share a counter (accepted, D-24)."""
    peer = request.client.host if request.client else None
    if not config.ON_CLOUD_RUN:
        return peer
    lines = request.headers.getlist("x-forwarded-for")
    if not lines:
        return peer
    last = ",".join(lines).split(",")[-1].strip()
    try:
        return str(ipaddress.ip_address(last))
    except ValueError:
        return peer


@app.post("/api/session")
def open_session(request: Request, body: dict = Body):
    """Sign in by account (06.1 D-47; api/accounts.py), set a new session cookie and keep or create the session's own
    sandbox (spec §8; FLOW-01). A session that already has a sandbox keeps it under a rotated session id (ASVS V3);
    a new one starts in the account's seat. With a cookie the sign-in is keyed (key, old session, '', "sign-in"; the
    body hash covers the account only, never the passcode). Wrong passcodes spend the per-client `passcode` cap
    (WR-03), and the 401 says how many this address has left in the current window (attempts_left); the address is
    `client_address`'s (D-24)."""
    # wrong passcodes are capped per client address (WR-03): once the limit is reached every attempt is refused
    # before the comparison, so a guess after the limit learns nothing
    client = caps.client_scope(client_address(request))
    db.in_tx(lambda conn: caps.reached(conn, client, "passcode"))
    account = body.get("account")  # HQ only against HQ_PASSCODE, a store code only against DEMO_PASSCODE (D-47)
    if not accounts.check_credential(account, body.get("passcode")):  # trimmed, compare_digest; blank opens nothing
        # its own transaction: the 401 keeps the count
        left = db.in_tx(lambda conn: caps.spend(conn, client, "passcode"))
        raise ApiError(401, "bad_passcode", "That passcode is not right",
                       **({"attempts_left": left} if left is not None else {}))
    sid = request.cookies.get(COOKIE)
    key = _request_key(request) if sid else None  # without a cookie no key row is kept (contract, accepted)

    def work(conn):
        if key:  # the key row first, as _write: a repeat returns the first answer and its rotated session
            stored = accounts.claim(conn, key, sid, body_hash({"account": account}))
            if stored is not None:
                return stored["session"], stored["answer"]
        s = db.one(conn, "SELECT * FROM sessions WHERE session_id = :s FOR UPDATE", s=sid) if sid else None
        if s and s["run_id"]:  # the workspace is kept; the account is checked against its own reference data
            dv = db.one(conn, "SELECT dataset_version FROM demo_runs WHERE run_id = :r", r=s["run_id"])
            acct, role, branch = accounts.resolve_account(refdata.get(dv["dataset_version"]), account)
            new, ms = accounts.rotate(conn, s, acct, role, branch), s["last_reset_ms"]
        else:
            template = sandbox.active_template(conn)
            if template is None:
                raise ApiError(503, "preparing", PREPARING)
            acct, role, branch = accounts.resolve_account(refdata.get(template["dataset_version"]), account)
            new = accounts.rotate(conn, s, acct, role, branch) if s else accounts.open_row(conn, acct, role, branch)
            caps.spend(conn, None, "sandbox")  # the daily cap on new sandboxes (FLOW-11); a 429 rolls everything back
            _, ms = sandbox.reset(conn, new)
        answer = {"ok": True, "reset_ms": ms, "account": acct, "role": role, "branch": branch}
        if key:
            accounts.remember(conn, key, sid, {"session": new, "answer": answer})
        return new, answer
    new, answer = db.in_tx(work)
    _prune()
    resp = JSONResponse(answer)
    resp.set_cookie(COOKIE, new, httponly=True, samesite="lax",
                    secure=config.ON_CLOUD_RUN or request.url.scheme == "https", max_age=COOKIE_MAX_AGE_S)
    return resp


@app.get("/api/session")
def session_info(s: dict = Depends(ready.session_or_paused)):
    return _read(s, lambda conn, run, actor, s: views.session_info(conn, s, run))


@app.post("/api/session/sign-out")
def sign_out(request: Request, s: dict = Depends(workspace_session), body: dict = Body):
    """Clear the account and keep the workspace (06.1 D-02): a session-scoped key, the session row locked. It needs a
    workspace, not an account, so a retry with its key after the account is cleared still gets the first answer."""
    def fn(conn, run, actor, s):
        db.one(conn, "SELECT session_id FROM sessions WHERE session_id = :s FOR UPDATE", s=s["session_id"])
        return accounts.sign_out(conn, s)
    return _write(request, s, "sign-out", body, fn, run_scoped=False)


# --- demo controls --------------------------------------------------------------------------------------------------

def _demo_only() -> None:
    """The demo controls answer only in demo mode (06.1 D-62; config.DEMO_MODE, off by default): 403 forbidden
    otherwise, the first statement of each, before the key is read or anything is written."""
    if not config.DEMO_MODE:
        raise ApiError(403, "forbidden", NOT_HERE)


@app.post("/api/demo/reset")
def reset(request: Request, s: dict = Session, body: dict = Body):
    """A fresh clone of the ACTIVE template in one transaction; the old sandbox is RETIRED and the seat is kept. The
    key is session-scoped, so a retried reset returns the first answer and never resets twice (A13). It spends the
    session's reset cap, then the daily sandbox cap (FLOW-11); a 429 rolls the counts and the key back. Demo mode
    only."""
    _demo_only()

    def fn(conn, run, actor, s):
        caps.spend(conn, s["session_id"], "reset")
        caps.spend(conn, s["session_id"], "sandbox")
        run_id, ms = sandbox.reset(conn, s["session_id"])
        return {"run_id": run_id, "reset_ms": ms}
    out = _write(request, s, "reset", body, fn, run_scoped=False)
    _prune()
    return out


@app.post("/api/demo/advance-day")
def advance_day(request: Request, s: dict = Session, body: dict = Body):
    """Next day, with the run locked FOR UPDATE; a stale expected_version answers the current state unchanged. Demo
    mode only."""
    _demo_only()
    expected = _whole(body, "expected_version")
    return _write(request, s, "advance-day", body,
                  lambda conn, run, actor, s: advance.advance_day(conn, run["run_id"], expected), mode="UPDATE")


# --- cases ----------------------------------------------------------------------------------------------------------

def _query_text(request: Request, name: str) -> str | None:
    """A query parameter as text (None when absent); a repeated, blank, over-long or NUL-holding one is 400."""
    values = request.query_params.getlist(name)
    if not values:
        return None
    value = values[0]
    if len(values) > 1 or not value.strip() or len(value) > KEY_MAX or "\x00" in value:
        raise ApiError(400, "bad_request", f"{name} is malformed")
    return value.strip()


def _query_int(request: Request, name: str, low: int = -INT_MAX, high: int = INT_MAX) -> int | None:
    """A query parameter as a whole number in [low, high] (None when absent); anything else is 400 bad_request."""
    value = _query_text(request, name)
    if value is None:
        return None
    if not re.fullmatch(r"-?[0-9]{1,10}", value) or not low <= int(value) <= high:
        raise ApiError(400, "bad_request", f"{name} must be a whole number from {low} to {high}")
    return int(value)


@app.get("/api/cases")
def board(request: Request, s: dict = Session):
    """The board (05.2 D-26): the seat's scope, HQ's `branch` and either seat's `risk` filter, one `page` of
    `page_size` items; the 06.1 filters `owner=me`, `product=<code>`, `deadline=scenario_date|none` and `blocked=1`
    (D-17, D-23), applied on the server before paging. Every parameter is checked here or against the reference
    settings (400 bad_request); `report` resolves Reports contributions with strict branch scope (08 D-18)."""
    branch, risk = _query_text(request, "branch"), _query_text(request, "risk")
    if risk is not None and risk not in cases.RISKS:
        raise ApiError(400, "bad_request", f"risk is one of {', '.join(cases.RISKS)}")
    owner, product = _query_text(request, "owner"), _query_text(request, "product")
    deadline, blocked = _query_text(request, "deadline"), _query_text(request, "blocked")
    report = _query_text(request, "report")
    cases.check_filters(owner, deadline, blocked, report)
    page, size = _query_int(request, "page", 1), _query_int(request, "page_size", 1)
    return _read(s, lambda conn, run, actor, s: {"day": run["day"], "version": run["version"], **cases.board_page(
        conn, run, actor, branch=branch, risk=risk, page=page or 1, page_size=size, owner=owner, product=product,
        deadline=deadline, blocked=blocked, report=report)})


@app.get("/api/series/{store}/{sku}")
def series(request: Request, store: str, sku: str, s: dict = Session):
    """One series' public history and forecast for the window `from`..`to` (day indexes, both required; 05.2 D-11)."""
    lo, hi = _query_int(request, "from"), _query_int(request, "to")
    if lo is None or hi is None:
        raise ApiError(400, "bad_request", "from and to are required day numbers")
    return _read(s, lambda conn, run, actor, s: views.series(conn, run, store, sku, lo, hi))


@app.get("/api/cases/{case_id}")
def case_detail(case_id: str, s: dict = Session):
    return _read(s, lambda conn, run, actor, s: cases.detail(conn, run, case_id))


@app.post("/api/cases/{case_id}/send")
def send(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    v = _text(body, "rec_version")
    return _write(request, s, f"send:{case_id}", body,
                  lambda conn, run, actor, s: workflow.send(conn, run, actor, case_id, v))


@app.get("/api/cases/{case_id}/activity")
def case_activity(case_id: str, s: dict = Session):
    return _read(s, lambda conn, run, actor, s: views.activity(conn, run, case_id))


@app.get("/api/cases/{case_id}/export")
def case_export(case_id: str, s: dict = Session):
    """The export bundle as a JSON download named after the case key; a read, so it never calls the worker."""
    bundle = _read(s, lambda conn, run, actor, s: views.export(conn, run, case_id))
    return JSONResponse(_jsonable(bundle),
                        headers={"Content-Disposition": f'attachment; filename="{bundle["case"]}.json"'})


@app.post("/api/cases/{case_id}/simulate")
def simulate(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    """A what-if on a stored version plus the overrides; it never changes the active plan (any seat)."""
    version = _text(body, "rec_version", required=False)
    return _write(request, s, f"simulate:{case_id}", body, lambda conn, run, actor, s: workflow.whatif(
        conn, run, case_id, body.get("overrides"), version))


@app.post("/api/cases/{case_id}/evaluate")
def evaluate_item(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    """Open a flagged item and run the engine on it (05.2 D-25): the case detail; any seat, as reading the case."""
    return _write(request, s, f"evaluate:{case_id}", body,
                  lambda conn, run, actor, s: flags.open_from_flag(conn, run, case_id))


@app.post("/api/cases/{case_id}/adopt")
def adopt(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    return _write(request, s, f"adopt:{case_id}", body, lambda conn, run, actor, s: workflow.adopt(
        conn, run, actor, case_id, body.get("overrides"), body.get("note"),
        rec_version=body.get("rec_version"), plan_key=body.get("plan_key")))


@app.post("/api/cases/{case_id}/decision")
def decision(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    version, choice = _text(body, "rec_version"), _text(body, "decision")
    reason = _text(body, "reason", required=False)
    acknowledged = body.get("acknowledged")  # {rec_version, plan_key, fact_ids} on approve and accept (07-CONTEXT D-26)
    plan_key = _text(body, "plan_key", required=False)  # the plan the page shows (07 review C; 07-CONTEXT D-23, D-32)
    return _write(request, s, f"decision:{case_id}", body, lambda conn, run, actor, s: workflow.decide(
        conn, run, actor, case_id, version, choice, reason, acknowledged, plan_key))


@app.post("/api/cases/{case_id}/prepare")
def prepare(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    return _write(request, s, f"prepare:{case_id}", body,
                  lambda conn, run, actor, s: workflow.prepare(conn, run, actor, case_id))


@app.post("/api/cases/{case_id}/reject")
def reject(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    return _write(request, s, f"reject:{case_id}", body,
                  lambda conn, run, actor, s: workflow.reject(conn, run, actor, case_id, body.get("reason")))


# --- the count path -------------------------------------------------------------------------------------------------

@app.post("/api/cases/{case_id}/requests")
def request_count(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    """The source manager asks the branch holding the lots to check a flagged fact (D-20; no cap spent): a count or a
    printed expiry date (`fact_id`), or both asked together (`fact_ids`, one or two; 07-CONTEXT D-11).
    A draft supplies its immutable fact binding; only its final human message is editable (09 D-23)."""
    version = _text(body, "rec_version")
    draft_id = _text(body, "draft_id") if "draft_id" in body else None
    message = _text(body, "message") if "message" in body else None
    plan_key = _text(body, "plan_key") if "plan_key" in body else None
    fact_ids = body.get("fact_ids")
    if "draft_id" in body:
        if set(body) != {"draft_id", "rec_version", "plan_key", "message"}:
            raise ApiError(400, "bad_request", "Send the draft binding and final message without changing its context")
        fact_id = None
    elif fact_ids is None:
        fact_id = _text(body, "fact_id")
    elif (not isinstance(fact_ids, list) or not 1 <= len(fact_ids) <= 2
          or not all(isinstance(f, str) for f in fact_ids)) or body.get("fact_id") is not None:
        raise ApiError(400, "bad_request", "fact_ids lists one or two facts, in place of fact_id")
    else:
        fact_id = None
    recount_of = _text(body, "recount_of", required=False)  # the request this one replaces (Ask again, D-07)
    return _write(request, s, f"requests:{case_id}", body, lambda conn, run, actor, s: signals.request_evidence(
        conn, run, actor, case_id, version, fact_id, recount_of=recount_of, fact_ids=fact_ids,
        draft_id=draft_id, message=message, plan_key=plan_key))


@app.get("/api/requests")
def requests_inbox(s: dict = Session):
    return _read(s, lambda conn, run, actor, s: signals.inbox(conn, run, actor))


@app.post("/api/requests/{request_id}/answer")
def answer_request(request: Request, request_id: str, s: dict = Session, form: dict = Depends(answer_form)):
    """The addressed manager answers with per-lot shelf and backroom counts (multipart), states when it checked and
    may attach one file. The body hash covers text, counts, observed and the file's sha256 and size (the bytes travel
    beside it)."""
    body = {k: v for k, v in form.items() if k != "file"}
    return _write(request, s, f"answer:{request_id}", body, lambda conn, run, actor, s: signals.answer(
        conn, run, actor, request_id, form["text"], form["counts"], form["observed"], form["file"], form["dates"],
        form["checked_lot"]))


@app.post("/api/signals/{signal_id}/confirm")
def confirm_count(request: Request, signal_id: str, s: dict = Session, body: dict = Body):
    """The addressed manager confirms a complete count on its current revision, with the scope tick. Approves and
    holds nothing. The run is locked FOR UPDATE, so no commit or prepare rechecks while the record changes (WR-01)."""
    revision = _whole(body, "revision")

    def work(conn, run, actor, s):  # a field report goes to its owner's confirmation (07-13; 07-CONTEXT D-15)
        if reports.is_report(conn, run, signal_id):  # a STOCK_COUNT report takes the tick as an answer does (07-14)
            return reports.confirm(conn, run, actor, signal_id, revision, body.get("checked_both"))
        return signals.confirm(conn, run, actor, signal_id, revision, body.get("checked_both"))
    return _write(request, s, f"confirm:{signal_id}", body, work, mode="UPDATE")


# --- verify before you move (Phase 7) -------------------------------------------------------------------------------

@app.post("/api/requests/{request_id}/cant-check")
def cant_check(request: Request, request_id: str, s: dict = Session, body: dict = Body):
    """The addressed manager closes the response attempt with Can't check; the fact stays unverified (07-CONTEXT
    D-07). Body `{}`."""
    return _write(request, s, f"cant-check:{request_id}", body, lambda conn, run, actor, s: signals.cant_check(
        conn, run, actor, request_id))


@app.get("/api/tasks")
def tasks(request: Request, s: dict = Session):
    """The seat's Tasks: addressed requests and the tile counts, read at one now() (07-CONTEXT D-04), plus the six
    tiles and one page of the one task list for `view` (open or closed) and `tile`, in the server's order
    (api/tasklist.py; D-36; 07-19)."""
    view, tile = _query_text(request, "view") or "open", _query_text(request, "tile")
    page, size = _query_int(request, "page", 1), _query_int(request, "page_size", 1)
    from . import tasklist  # it reads signals, execution and movement at import
    return _read(s, lambda conn, run, actor, s: tasklist.view(conn, run, actor, view=view, tile=tile, page=page or 1,
                                                              page_size=size))


@app.get("/api/signals/{signal_id}/file")
def signal_file(signal_id: str, s: dict = Session):
    """A signal's file, found by the session's workspace only (another workspace's is 404), read back through the API
    with the stored type, nosniff, private no-store and attachment for a PDF; never a redirect or a bucket URL
    (07-CONTEXT D-20; acceptance check 15). Only a seat party to the item reads it (`files.may_read`, else 403). The
    bytes are read after the transaction, holding no connection."""
    def party(conn, run, actor, s):
        found = files.lookup(conn, run, signal_id)
        files.may_read(actor, found["signal"], found["request"], found["case"])
        return found
    found = _read(s, party)
    return Response(files.read(found["uri"]), media_type=found["mime"],
                    headers=files.headers(found["mime"], found["signal_id"]))


async def _report_parts(request: Request, names: tuple[str, ...]) -> tuple[dict, bytes]:
    """A report form's text parts and its one optional file part (07-CONTEXT D-14, D-20), read as the answer's are:
    the body up to `uploads.max_bytes` + `uploads.form_bytes` (research F-5), the text parts up to
    `uploads.form_bytes`; a file part over `uploads.max_bytes` is 413 too_large, one of another type 415 bad_file
    (sniffed before anything is written), a second file part (or a file under another name) 400. An empty file part
    is no file. Anything malformed is 400."""
    ctype = request.headers.get("content-type", "")
    if not ctype.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        raise ApiError(400, "bad_request", "Send the report as multipart/form-data")
    await bounded_body(request, config.UPLOAD_MAX_BYTES + config.UPLOAD_FORM_BYTES)
    form = await request.form(max_files=1, max_fields=20)
    upload = form.get("file")
    if isinstance(upload, str):  # a field, not a file part: blank is no file (an empty file input), else 400
        if upload:
            raise ApiError(400, "bad_request", "Attach the file as a file part")
        upload = None
    if any(not isinstance(v, str) for k, v in form.multi_items() if k != "file") or len(form.getlist("file")) > 1:
        raise ApiError(400, "bad_request", "Attach one file, as the file part")
    if sum(len(v.encode("utf-8")) for _, v in form.multi_items() if isinstance(v, str)) > config.UPLOAD_FORM_BYTES:
        raise ApiError(400, "bad_request", TOO_LARGE)
    if any(len(form.getlist(n)) > 1 for n in names) or any("\x00" in (form.get(n) or "") for n in names):
        raise ApiError(400, "bad_request", "The report is malformed")
    data = await upload.read(config.UPLOAD_MAX_BYTES + 1) if upload is not None else b""
    if len(data) > config.UPLOAD_MAX_BYTES:
        raise files.too_large()
    if data:
        files.sniff(data)  # 415 before anything is written (the service sniffs again before it stores)
    return {n: form.get(n) for n in names}, data


def _report_fields(raw: str | None) -> dict | None:
    """A form's `fields` part as the kind's JSON object; None when blank (PATCH: no field to correct)."""
    if raw is None or not raw.strip():
        return None
    try:
        fields = json.loads(raw)
    except (ValueError, RecursionError):
        raise ApiError(400, "bad_request", "fields must be a JSON object of the report's fields") from None
    if not isinstance(fields, dict):
        raise ApiError(400, "bad_request", "fields must be a JSON object of the report's fields")
    return fields


def _file_keys(data: bytes) -> dict:
    """The file beside a form's fields: its bytes travel apart, its sha256 and size go into the body hash, so a
    retried key with another file is 409 idempotency_conflict."""
    return {"file": data or None, "file_sha256": hashlib.sha256(data).hexdigest() if data else None,
            "file_size": len(data) or None}


async def report_form(request: Request) -> dict:
    """A field report's multipart fields (07-CONTEXT D-14): `kind`, `fields` (a string holding the kind's JSON
    object), `text` (may be empty), `case_id` and `corrects` (blank is none), `observed` ("When did you check?" for
    STOCK_COUNT and EXPIRY: "now" or an ISO-8601 time with its offset; absent is None, never a default, D-22) and one
    optional `file` part (PNG, JPEG or PDF by content, at most `uploads.max_bytes`; 07-14), read by `_report_parts`."""
    parts, data = await _report_parts(request, ("kind", "fields", "text", "case_id", "corrects", "observed"))
    return {"kind": parts["kind"], "fields": _report_fields(parts["fields"]) or {}, "text": parts["text"] or "",
            "case_id": parts["case_id"] or None, "corrects": parts["corrects"] or None,
            "observed": parts["observed"], **_file_keys(data)}


async def patch_form(request: Request) -> dict:
    """PATCH /api/signals/{id}'s body (07-CONTEXT D-15, D-20): JSON `{"revision", "fields"?, "text"?, "observed"?}`,
    or the same as multipart parts (`revision` a whole number as text, `fields` a JSON object as text) with one
    optional `file` part, read by `_report_parts`. Anything malformed is 400."""
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await json_body(request)
        for name in ("text", "observed"):
            if body.get(name) is not None and not isinstance(body[name], str):
                raise ApiError(400, "bad_request", f"{name} must be text")
        if body.get("fields") is not None and not isinstance(body["fields"], dict):
            raise ApiError(400, "bad_request", "fields must be a JSON object of the report's fields")
        return {"revision": _whole(body, "revision"), "fields": body.get("fields"), "text": body.get("text"),
                "observed": body.get("observed"), **_file_keys(b"")}
    parts, data = await _report_parts(request, ("revision", "fields", "text", "observed"))
    raw = (parts["revision"] or "").strip()
    if not re.fullmatch(r"[0-9]{1,9}", raw):
        raise ApiError(400, "bad_request", "revision is required as a whole number")
    return {"revision": int(raw), "fields": _report_fields(parts["fields"]), "text": parts["text"],
            "observed": parts["observed"], **_file_keys(data)}


@app.post("/api/signals")
def file_report(request: Request, s: dict = Session, form: dict = Depends(report_form)):
    """A manager files a field report for their own store, HQ a transport report (07-CONTEXT D-14, D-15): READ,
    applied to nothing until its owner confirms it. It spends the workspace's report allowance (scope run:<run_id>,
    D-20) in the same transaction, so a refused report and a retried key spend nothing. The run is locked FOR UPDATE
    (the report records the live cases in its scope)."""
    def work(conn, run, actor, s):
        caps.spend(conn, reports.workspace_scope(run), "signal")
        return reports.create(conn, run, actor, form["kind"], form["fields"], form["text"], form["case_id"],
                              form["corrects"], form["observed"], form["file"])
    body = {k: v for k, v in form.items() if k != "file"}  # the file's sha256 and size stand in for its bytes
    return _write(request, s, "report", body, work, mode="UPDATE")


@app.get("/api/signals")
def list_reports(request: Request, s: dict = Session):
    """The seat's field reports (HQ: every store's), newest first, one row per report, in pages (`page`,
    `page_size`), with the workspace's allowance (D-20); `state` and `kind` filter them."""
    state, kind = _query_text(request, "state"), _query_text(request, "kind")
    page, size = _query_int(request, "page", 1) or 1, _query_int(request, "page_size", 1)
    mine, store, case = _query_text(request, "mine"), _query_text(request, "store"), _query_text(request, "case")
    if state is not None and state not in reports.STATES:
        raise ApiError(400, "bad_request", f"state must be one of {', '.join(reports.STATES)}")
    if kind is not None and kind not in reports.KINDS:
        raise ApiError(400, "bad_request", f"kind must be one of {', '.join(reports.KINDS)}")
    if mine not in (None, "1"):
        raise ApiError(400, "bad_request", "mine must be 1")
    return _read(s, lambda conn, run, actor, s: reports.listing(conn, run, actor, state, kind, page, size,
                                                                mine == "1", store, case))


@app.post("/api/signals/preview")
def preview_draft(request: Request, s: dict = Session, body: dict = Body):
    """The impact preview of a report not filed yet (07-CONTEXT D-16; 07-20, the Report an observation sheet):
    `{"kind", "fields", "corrects"?}` from a seat that may file it. The stored preview's shape with signal_id,
    report_id and revision null; it files nothing, stores no report, spends only the `preview` cap (before any engine
    run, and none when every what-if is already stored: impact._computed) and changes nothing active (the what-if versions are stored with is_whatif true, as for a filed
    report's preview). The run is locked FOR UPDATE."""
    fields, corrects = body.get("fields"), body.get("corrects")
    if not isinstance(fields, dict):
        raise ApiError(400, "bad_request", "fields must be a JSON object of the report's fields")
    if corrects is not None and not isinstance(corrects, str):
        raise ApiError(400, "bad_request", "corrects must name a report")
    return _write(request, s, "preview-draft", body, lambda conn, run, actor, s: impact.draft(
        conn, run, actor, body.get("kind"), fields, corrects), mode="UPDATE")


@app.get("/api/signals/{signal_id}")
def read_report(signal_id: str, s: dict = Session):
    """A field report with its revisions, for a seat party to it (07-CONTEXT D-14)."""
    return _read(s, lambda conn, run, actor, s: reports.get(conn, run, actor, signal_id))


@app.patch("/api/signals/{signal_id}")
def edit_report(request: Request, signal_id: str, s: dict = Session, form: dict = Depends(patch_form)):
    """The reporter or the owner edits a report before confirmation (07-CONTEXT D-15, D-20): a new revision of the
    same report, the earlier one REPLACED; JSON or multipart with one optional file. The body hash covers the
    revision, fields, text, observed and the file's sha256 and size. The run is locked FOR UPDATE (the revision
    records the live cases in its scope)."""
    body = {k: v for k, v in form.items() if k != "file"}
    return _write(request, s, f"edit:{signal_id}", body, lambda conn, run, actor, s: reports.patch(
        conn, run, actor, signal_id, form["revision"], form["fields"], form["text"], form["observed"],
        form["file"]), mode="UPDATE")


@app.post("/api/signals/{signal_id}/preview")
def preview_report(request: Request, signal_id: str, s: dict = Session, body: dict = Body):
    """The impact preview (07-CONTEXT D-16): `{"revision"}` from a seat party to the report; up to
    reports.preview_cases affected live cases re-run as what-ifs, labelled "Potential impact · unconfirmed", stored
    per revision. Changes nothing, blocks nothing, spends only the `preview` cap (impact._computed). The run is locked
    FOR UPDATE, so two previews of one revision run the engine once."""
    revision = _whole(body, "revision")
    return _write(request, s, f"preview:{signal_id}", body, lambda conn, run, actor, s: impact.preview(
        conn, run, actor, signal_id, revision), mode="UPDATE")


@app.post("/api/signals/{signal_id}/withdraw")
def withdraw_report(request: Request, signal_id: str, s: dict = Session, body: dict = Body):
    """The reporter withdraws a report before confirmation (07-CONTEXT D-15): `{}`; no operational effect."""
    return _write(request, s, f"withdraw:{signal_id}", body, lambda conn, run, actor, s: reports.withdraw(
        conn, run, actor, signal_id))


@app.post("/api/signals/{signal_id}/dismiss")
def dismiss_report(request: Request, signal_id: str, s: dict = Session, body: dict = Body):
    """The owner dismisses a report (07-CONTEXT D-15): `{"reason"?}`; never applied, the history kept."""
    return _write(request, s, f"dismiss:{signal_id}", body, lambda conn, run, actor, s: reports.dismiss(
        conn, run, actor, signal_id, body.get("reason")))


@app.post("/api/signals/{signal_id}/done")
def mark_task_done(request: Request, signal_id: str, s: dict = Session, body: dict = Body):
    """Mark done (07-CONTEXT D-13): `{}` from the report branch's manager closes an advisory task (a confirmed display
    issue); who and when are recorded, its history is kept, and no hold is touched."""
    return _write(request, s, f"done:{signal_id}", body, lambda conn, run, actor, s: reports.mark_done(
        conn, run, actor, signal_id))


@app.post("/api/lots/{lot_id}/holds")
def place_hold(request: Request, lot_id: str, s: dict = Session, body: dict = Body):
    """The lot's branch manager holds the lot for review (a CONDITION hold, 07-CONTEXT D-13): `{"reason",
    "signal_id"?}`; the active one when it exists. Only the lot's manager places it (the seat from the session)."""
    reason, signal_id = _text(body, "reason"), _text(body, "signal_id", required=False)
    return _write(request, s, f"holds:{lot_id}", body, lambda conn, run, actor, s: lotholds.place(
        conn, run, actor, lot_id, "CONDITION", reason, signal_id=signal_id))


@app.post("/api/holds/{hold_id}/release")
def release_hold(request: Request, hold_id: str, s: dict = Session, body: dict = Body):
    """The lot's branch manager inspects and releases a hold with a note (07-CONTEXT D-13): `{"note"}`. Nothing else
    releases or clears a hold."""
    note = _text(body, "note")
    return _write(request, s, f"release:{hold_id}", body, lambda conn, run, actor, s: lotholds.release(
        conn, run, actor, hold_id, note))


@app.post("/api/cases/{case_id}/review")
def review_keep(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    """"Reviewed · keep plan" (07-CONTEXT D-17): `{"flag_ids", "note"}` from a person authorised to approve the case;
    exactly those open review flags clear. Never clears a hold or settles a Required check."""
    return _write(request, s, f"review:{case_id}", body, lambda conn, run, actor, s: reviewflags.keep(
        conn, run, actor, case_id, body.get("flag_ids"), body.get("note")))


@app.post("/api/cases/{case_id}/select")
def select_plan(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    """Select another plan (07-CONTEXT D-23): `{"rec_version", "plan_key"}` from the source manager; only a row key of
    the active version's engine options, rechecked, with its own fact checks; a pending round is cancelled. Never
    clears a lot hold."""
    version, plan_key = _text(body, "rec_version"), _text(body, "plan_key")
    return _write(request, s, f"select:{case_id}", body, lambda conn, run, actor, s: selection.select(
        conn, run, actor, case_id, version, plan_key))


@app.post("/api/cases/{case_id}/cancel")
def cancel_plan(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    """Cancel approved plan (07-CONTEXT D-23, D-25): `{"reason"}` from a person authorised to approve the plan (the
    source manager, or the destination manager of a transfer); APPROVED or PREPARED only. Stored CLOSED with closed_as
    CANCELLED; the work orders and decisions stay in history, marked cancelled; the reservations are released."""
    return _write(request, s, f"cancel:{case_id}", body, lambda conn, run, actor, s: closing.cancel(
        conn, run, actor, case_id, body.get("reason")))


@app.post("/api/cases/{case_id}/reassess")
def reassess_lot(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    """Reassess (07-CONTEXT D-24): `{}` from the closed case's source manager; REJECTED, or CLOSED as CANCELLED. A lot
    still on the record with stock that still qualifies gets one successor case, linked → {case_id, key}; otherwise
    409 not_at_risk, and the case's stored lot_status becomes null (in a transaction of its own, since the refusal
    rolls the write back)."""
    try:
        return _write(request, s, f"reassess:{case_id}", body, lambda conn, run, actor, s: closing.reassess(
            conn, run, actor, case_id))
    except ApiError as exc:
        if exc.code == "not_at_risk":
            db.in_tx(lambda conn: closing.forget(conn, db.lock_run(conn, s["run_id"]), case_id))
        raise


# --- policy settings (D-09, D-23) -----------------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings(s: dict = Session):
    return _read(s, lambda conn, run, actor, s: settings.read_settings(conn, run))


@app.patch("/api/settings")
def patch_settings(request: Request, s: dict = Session, body: dict = Body):
    """Any seat, demo mode only: the three writable Policy fields of this sandbox, with the run locked FOR UPDATE and
    its version checked; live cases re-evaluate (a pending approval is cancelled)."""
    expected = _whole(body, "expected_version")
    changes = body.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise ApiError(400, "bad_request", "Name at least one setting to change")
    return _write(request, s, "settings", body, lambda conn, run, actor, s: settings.write_settings(
        conn, run, actor, changes, expected), mode="UPDATE")


# --- route modules (06.1) -------------------------------------------------------------------------------------------

_route_modules: set[str] = set()  # the routes_ modules already imported: a second call registers nothing twice


def load_route_modules() -> None:
    """Import every `api/routes_*.py` module once, in name order, so its routes register on `app` (06.1: the route
    plans add a module and never edit this file for their routes).

    Only modules inside this package's own directory are found (`pkgutil.iter_modules` over it), imported by their
    package name. A route module imports `app`, `_read`, `_write`, `Session`, `Body`, `_text` and `_whole` from `.app`
    (all defined above this point); no other module imports a routes_ module, so this is the one place they load."""
    here = pathlib.Path(__file__).parent
    for info in sorted(pkgutil.iter_modules([str(here)]), key=lambda m: m.name):
        if not info.name.startswith("routes_") or info.name in _route_modules:
            continue
        importlib.import_module(f"{__package__}.{info.name}")
        _route_modules.add(info.name)
    _keep_spa_last()


def _keep_spa_last() -> None:
    """The SPA catch-all (`spa`, registered below) stays the last route, so a routes_ module loaded after it still
    answers its /api paths first (OPS-03). Before `spa` exists there is nothing to move."""
    spa_route = globals().get("spa")
    if spa_route is None:
        return
    routes = app.router.routes
    tail = [r for r in routes if getattr(r, "endpoint", None) is spa_route]
    routes[:] = [r for r in routes if r not in tail] + tail


load_route_modules()


# --- the built web app (OPS-03): the last route ----------------------------------------------------------------------

NO_CACHE = {"Cache-Control": "no-cache"}  # index.html is revalidated, so a new release reaches every browser


def spa(request: Request):
    """A file of the web build when the path resolves to one inside it, else index.html (no-cache); /api paths no
    route serves stay the 404 envelope. Containment is `is_relative_to` on resolved paths, so "..", encoded slashes,
    an absolute path, a sibling such as dist-old and a symlink out of the build all land on index.html; an index.html
    that itself resolves outside the build counts as no build (06-REVIEWS R2-12)."""
    path = request.path_params["path"]
    if path == "api" or path.startswith("api/"):
        raise ApiError(404, "not_found", "No such route")
    try:
        root = pathlib.Path(config.WEB_DIST).resolve()
        index = (root / "index.html").resolve()
        usable = index.is_relative_to(root) and index.is_file()
    except (OSError, RuntimeError, ValueError):  # RuntimeError: a symlink loop
        usable = False
    if not usable:
        raise ApiError(404, "not_found", "No such route")  # no build: API-only runs and tests
    try:
        target = (root / path).resolve()
        if path and target != index and target.is_relative_to(root) and target.is_file():
            return FileResponse(target)
    except (OSError, RuntimeError, ValueError):  # e.g. an embedded NUL: fall through to index.html
        pass
    return FileResponse(index, headers=NO_CACHE)


# A plain Starlette Route, registered last so every /api route answers first (and the APIRoute census is unchanged).
app.add_route("/{path:path}", spa, methods=["GET", "HEAD"])
