"""Settings: the environment wiring and the product's policy values. Code reads them as `config.X` at call time, so
tests can monkeypatch.

The environment holds only deployment wiring: where the database is, the passcode, the modes, whether the app runs
as a Cloud Run service (`K_SERVICE`, read as `ON_CLOUD_RUN`; 06-CONTEXT D-24), the connection pool and where the built
web app lives (`WEB_DIST`, served by the API's last route).
The pool defaults fit a Cloud SQL db-f1-micro (25 connections): 5 + 2 overflow per process, with an explicit checkout
wait so a busy pool fails as a plain 503 instead of hanging (CLAUDE.md stack notes; 04-RESEARCH Pitfall 12). Gemini,
Cloud Storage, uploads and the contact address arrive with their phases (7, 9, 11).

Every policy value (the hold TTL, the engine timeout and queue wait, the FLOW-11 caps and their time zone, the count
request lead and limit, the request body bound) is a `{value, source, type}` leaf in api/settings.json, and the bounds
of the three writable Policy fields are leaves in api/settings_bounds.json (04-CONTEXT D-08, D-09, D-23). Both are
checked by the same leaf checker as reference data (`data.config.leaves`), which names the dotted path of a malformed
leaf. The ESL-02 case-opening thresholds are read from reference data (`world.case_opening`, 03-CONTEXT D-22). None of
these is a code constant; only the hold TTL, the engine timeout and the daily AI cap have environment overrides
(deploy-time knobs).
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from data import config as refconfig
from engine.model import Policy

env = os.environ.get
HERE = pathlib.Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "settings.json"
BOUNDS_FILE = HERE / "settings_bounds.json"
# The cap windows usage_counts knows (04-API-CONTRACT Caps); minute is the wrong-passcode block's (06-CONTEXT D-24).
WINDOWS = ("minute", "hour", "day")

DATABASE_URL = env("DATABASE_URL", "postgresql+pg8000://postgres@localhost:5432/shelfshift")
CLOUD_SQL_INSTANCE = env("CLOUD_SQL_INSTANCE", "")  # project:region:instance; set on Cloud Run (IAM login)
DB_USER = env("DB_USER", "")  # the service account's IAM database user, without .gserviceaccount.com
# The runtime service account's IAM database user (its email without .gserviceaccount.com). Set only on the deploy
# job, so migrate() grants it what the app needs on every table and sequence (06-RESEARCH Pitfall 5); empty locally.
RUN_DB_USER = env("RUN_DB_USER", "")
DB_NAME = env("DB_NAME", "shelfshift")
# No passcode default in code: the deploy sets a secret, a local run its environment or the gitignored .env. Both
# sides are trimmed; a blank one makes the app refuse to start (app.lifespan) and opens no session (CR-01).
DEMO_PASSCODE = (env("DEMO_PASSCODE") or "").strip()
# HQ's own credential (06.1 D-47, D-64, D-68): never a default in code and never the store passcode. The app refuses
# to start when it is blank, shorter than HQ_MIN_LENGTH or equal to DEMO_PASSCODE (accounts.hq_config_problem).
HQ_PASSCODE = (env("HQ_PASSCODE") or "").strip()
HQ_MIN_LENGTH = 12  # characters (D-68): a security setting of this app, not a retailer value
# Demo mode (06.1 D-62): off unless the environment says "on". Only then do reset and advance-day answer; the
# test suite and the browser harness set it where they need Next day, and the deployed service runs with it off.
DEMO_MODE = env("DEMO_MODE", "off") == "on"
# Cloud Run sets K_SERVICE on every service instance and never on jobs (D-24). When it is set the session cookie is
# Secure and the wrong-passcode counter reads the rightmost X-Forwarded-For entry (app.client_address); Compose and the
# deploy scripts never set it, so a local run counts the TCP peer.
ON_CLOUD_RUN = bool(env("K_SERVICE", "").strip())
ENGINE_WORKER = env("ENGINE_WORKER", "on") == "on"  # engine searches in one worker process (OPS-06)
GIT_COMMIT = env("GIT_COMMIT", "dev")
# The built React app the API serves (/app/web/dist in the image); tests point it at a temporary build (OPS-03).
WEB_DIST = env("WEB_DIST", str(HERE.parent / "web" / "dist"))
# Public support contact, set at deploy; no default address (11-CONTEXT D-04).
CONTACT_EMAIL = env("CONTACT_EMAIL", "")
# Model and endpoint selected by the deploy owner (11-CONTEXT D-01).
GEMINI_MODEL = env("GEMINI_MODEL", "")
VERTEX_LOCATION = env("VERTEX_LOCATION", "")
# Cloud mode is explicitly enabled by deployment only.
GCP = env("GCP", "off") == "on"
# Upload wiring is shared with Phase 7; local paths are for local runs only.
UPLOADS_BUCKET = env("UPLOADS_BUCKET", "")
UPLOAD_DIR = env("UPLOAD_DIR", "") or os.path.join(tempfile.gettempdir(), "shelfshift-uploads")

# Connection pool (infrastructure, not retailer values): db-f1-micro allows 25 connections in all.
POOL_SIZE = int(env("POOL_SIZE", "5"))
MAX_OVERFLOW = int(env("MAX_OVERFLOW", "2"))
POOL_TIMEOUT_S = float(env("POOL_TIMEOUT_S", "10"))  # seconds a request waits for a free connection


def _number(path: str, value, integer: bool = False):
    """A positive number (a whole one when `integer`), else a ConfigError naming the dotted path."""
    ok = isinstance(value, int if integer else (int, float)) and not isinstance(value, bool) and value > 0
    if not ok:
        raise refconfig.ConfigError(f"{path}: must be a positive {'whole number' if integer else 'number'}")
    return value


def _time_zone(name) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise refconfig.ConfigError(f"settings.caps.time_zone: {name!r} is not an IANA time zone") from None


def load_settings(path: pathlib.Path | str = SETTINGS_FILE) -> dict:
    """api/settings.json as values, after checking every leaf and every value's shape (ConfigError names the path)."""
    tree = refconfig.read(pathlib.Path(path))
    refconfig.leaves(tree, "settings")
    s = refconfig.values(tree)
    try:
        _number("settings.holds.hold_ttl_min", s["holds"]["hold_ttl_min"])
        for key in ("timeout_s", "queue_wait_s"):
            _number(f"settings.engine.{key}", s["engine"][key])
        for group in ("session", "client"):
            for kind, cap in s["caps"][group].items():
                where = f"settings.caps.{group}.{kind}"
                if not isinstance(cap, dict) or set(cap) != {"limit", "window"} or cap["window"] not in WINDOWS:
                    raise refconfig.ConfigError(f"{where}: must be {{limit, window}} with window one of "
                                                f"{', '.join(WINDOWS)}")
                _number(f"{where}.limit", cap["limit"], integer=True)
        if set(s["caps"]["session"]) & set(s["caps"]["client"]):
            raise refconfig.ConfigError("settings.caps: a kind is capped per session or per client, not both")
        for kind, limit in s["caps"]["global"].items():
            _number(f"settings.caps.global.{kind}", limit, integer=True)
        _time_zone(s["caps"]["time_zone"])
        _number("settings.requests.lead_min", s["requests"]["lead_min"], integer=True)
        _number("settings.requests.max_per_version", s["requests"]["max_per_version"], integer=True)
        _number("settings.http.max_body_bytes", s["http"]["max_body_bytes"], integer=True)
        for key in ("retired_sandbox_hours", "session_key_days", "usage_window_days", "engine_cache_days",
                    "runs_per_batch", "rows_per_batch"):
            _number(f"settings.prune.{key}", s["prune"][key], integer=True)
    except KeyError as e:
        raise refconfig.ConfigError(f"settings: required value {e.args[0]!r} is missing") from None
    return s


def load_bounds(path: pathlib.Path | str = BOUNDS_FILE) -> dict:
    """api/settings_bounds.json as {field: {min, max | allowed, unit?}}: exactly the writable Policy fields, each a
    Policy leaf (D-09, D-23)."""
    tree = refconfig.read(pathlib.Path(path))
    found = refconfig.leaves(tree, "settings_bounds")
    policy_fields = {f.name for f in dataclasses.fields(Policy)}
    out = {}
    for where, leaf in found:
        field = where.split(".", 1)[1]
        bound = leaf["value"]
        if field not in policy_fields or leaf["type"] != "Policy":
            raise refconfig.ConfigError(f"{where}: must be an engine Policy field of type Policy")
        if not isinstance(bound, dict) or set(bound) not in ({"min", "max"}, {"allowed"}):
            raise refconfig.ConfigError(f"{where}: a bound is {{min, max}} or {{allowed}}")
        out[field] = {**bound, **({"unit": leaf["unit"]} if "unit" in leaf else {})}
    return out


def _env_or(name: str, value, cast):
    """`value` from the settings file, unless the environment sets `name` (a deploy-time knob)."""
    raw = env(name)
    return cast(raw if raw not in (None, "") else value)


SETTINGS = load_settings()
SETTINGS_BOUNDS = load_bounds()

HOLD_TTL_MIN = _env_or("HOLD_TTL_MIN", SETTINGS["holds"]["hold_ttl_min"], float)  # real minutes (spec §8)
ENGINE_TIMEOUT_S = _env_or("ENGINE_TIMEOUT_S", SETTINGS["engine"]["timeout_s"], float)
ENGINE_QUEUE_S = SETTINGS["engine"]["queue_wait_s"]
SESSION_CAPS = {kind: (cap["limit"], cap["window"]) for kind, cap in SETTINGS["caps"]["session"].items()}
CLIENT_CAPS = {kind: (cap["limit"], cap["window"]) for kind, cap in SETTINGS["caps"]["client"].items()}  # WR-03
GLOBAL_CAPS = dict(SETTINGS["caps"]["global"])
GLOBAL_CAPS["ai"] = _env_or("GLOBAL_AI_CAP", GLOBAL_CAPS["ai"], int)
CAP_TZ = _time_zone(SETTINGS["caps"]["time_zone"])  # daily windows start at local midnight here
REQUEST_LEAD_MIN = SETTINGS["requests"]["lead_min"]
MAX_REQUESTS_PER_VERSION = SETTINGS["requests"]["max_per_version"]
MAX_BODY_BYTES = SETTINGS["http"]["max_body_bytes"]  # a request body is read up to this many bytes (WR-02)
PRUNE = dict(SETTINGS["prune"])  # retention and batch bounds of what no live session can reach (IN-01)
CASE_OPENING = refconfig.load()["world"]["case_opening"]  # the ESL-02 thresholds, from reference data (D-22)
