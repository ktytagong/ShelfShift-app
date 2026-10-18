"""docker-compose.yml, read as text (no YAML library is pinned; DB-free, no Docker needed except the optional
`config` check): db, then a one-off init running the deploy job's command, then the app, all in engine-only mode on
loopback (06-CONTEXT D-23, OPS-01, 04-SECURITY F-2)."""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
TEXT = COMPOSE.read_text(encoding="utf-8")


def services() -> dict[str, str]:
    """{name: the service's block text} under the top-level `services:` key (two-space indented names)."""
    block = re.search(r"^services:\n((?:(?:[ \t].*|)\n?)*)", TEXT, flags=re.M)
    assert block, "no top-level services: key"
    found, name = {}, None
    for line in block.group(1).splitlines():
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            name = m.group(1)
            found[name] = ""
        elif name:
            found[name] += line + "\n"
    return found


def env_value(block: str, key: str) -> str | None:
    m = re.search(rf"^\s+{re.escape(key)}:\s*(.+?)\s*(?:#.*)?$", block, flags=re.M)
    return m.group(1).strip() if m else None


def test_db_init_and_app_exist():
    assert {"db", "init", "app"} <= set(services()), sorted(services())


def test_init_runs_the_deploy_job_command_after_a_healthy_db():
    init = services()["init"]
    assert re.search(r'^\s+command:\s*\["python",\s*"-m",\s*"api\.sandbox",\s*"build"\]\s*$', init, flags=re.M), init
    assert re.search(r"^\s+db:\s*\{\s*condition:\s*service_healthy\s*\}\s*$", init, flags=re.M), init
    assert re.search(r'^\s+restart:\s*"no"\s*$', init, flags=re.M), init


def test_the_app_waits_for_init_to_complete():
    app = services()["app"]
    assert re.search(r"^\s+init:\s*\{\s*condition:\s*service_completed_successfully\s*\}\s*$", app, flags=re.M), app
    assert re.search(r"^\s+db:\s*\{\s*condition:\s*service_healthy\s*\}\s*$", app, flags=re.M), app
    assert "command:" not in app, "the app runs the image command, never a build or migration"


@pytest.mark.parametrize("name", ["init", "app"])
def test_init_and_app_reach_the_db_by_service_name_with_gcp_off(name):
    block = services()[name]
    url = env_value(block, "DATABASE_URL")
    assert url and re.fullmatch(r"postgresql\+pg8000://postgres@db:5432/shelfshift", url), (name, url)
    assert env_value(block, "GCP") == '"off"', (name, env_value(block, "GCP"))
    assert env_value(block, "ENGINE_WORKER") == '"on"', name


def test_no_google_or_cloud_sql_setting():
    hits = [f"{n}: {line.strip()}" for n, line in enumerate(TEXT.splitlines(), 1)
            if "GOOGLE_" in line or "CLOUD_SQL_" in line]
    assert hits == [], hits


def test_every_published_port_is_flow_style_on_loopback_and_overridable():
    lines = [(n, line) for n, line in enumerate(TEXT.splitlines(), 1) if re.match(r"^\s*ports:", line)]
    assert len(lines) == 2, lines
    for n, line in lines:
        m = re.fullmatch(r"\s*ports:\s*\[(.*)\]\s*", line)
        assert m, f"line {n}: block-style or malformed ports: {line.strip()}"
        published = [p.strip().strip("'\"") for p in m.group(1).split(",")]
        assert published and all(p.startswith("127.0.0.1:") for p in published), f"line {n}: {published}"
    assert services()["db"].count('ports: ["127.0.0.1:${SS_DB_PORT:-5432}:5432"]') == 1, "db: SS_DB_PORT"
    assert services()["app"].count('ports: ["127.0.0.1:${SS_APP_PORT:-8080}:8080"]') == 1, "app: SS_APP_PORT"
    assert "ports:" not in services()["init"], "init publishes nothing"


def test_the_app_runs_product_mode_with_the_hq_passcode_defaulted():
    """A13: the local app mirrors the service (06.1 D-62): demo mode off, and HQ_PASSCODE beside DEMO_PASSCODE with the
    same `:-` default; the product app refuses a blank, short or duplicate HQ value at start."""
    app = services()["app"]
    assert env_value(app, "DEMO_MODE") == '"off"'
    assert env_value(app, "HQ_PASSCODE") == "${HQ_PASSCODE:-}"
    assert "HQ_PASSCODE" not in services()["init"] and "DEMO_MODE" not in services()["init"]


def test_compose_check_generates_both_credentials_in_memory_and_never_echoes_them():
    if shutil.which("make") is None:
        pytest.skip("make is not installed")
    r = subprocess.run(["make", "-n", "compose-check"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    text = r.stdout
    for name in ("DEMO_PASSCODE", "HQ_PASSCODE", "SMOKE_PASSCODE", "SMOKE_HQ_PASSCODE"):
        assert re.search(rf"\bexport\b[^\n;]*\b{name}\b", text), name
    assert text.count("secrets.token_urlsafe(18)") == 2  # both generated, the HQ one long enough (12+)
    assert not re.search(r"echo[^;\n]*\$\$?\{?(DEMO_PASSCODE|HQ_PASSCODE|SMOKE_)", text)
    assert not re.search(r"(DEMO|HQ|SMOKE_HQ|SMOKE)_PASSCODE=['\"]?[A-Za-z0-9_-]{8,}", text)  # no literal value


def test_the_passcode_has_a_default_and_nothing_is_required():
    """Every command, `make db-up` included, must work with DEMO_PASSCODE unset; the app refuses a blank one (CR-01)."""
    assert env_value(services()["app"], "DEMO_PASSCODE") == "${DEMO_PASSCODE:-}"
    assert ":?" not in TEXT, [line for line in TEXT.splitlines() if ":?" in line]
    assert "DEMO_PASSCODE" not in services()["init"], "init needs no passcode"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
def test_compose_config_succeeds_with_the_passcode_unset():
    env = {k: v for k, v in os.environ.items() if k not in ("DEMO_PASSCODE", "HQ_PASSCODE")}
    r = subprocess.run(["docker", "compose", "-f", str(COMPOSE), "config", "--quiet"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
