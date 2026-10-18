"""The image recipe, the uvicorn launchers and the ignore files, read as text (DB-free; no Docker needed).

- The image copies engine, data, api, reference and the web stage's dist only, runs as a non-root user and never
  migrates or builds a template at startup (OPS-05, 06-CONTEXT D-18).
- Every uvicorn launcher (the image, `make serve`, the browser harness's API child) runs with proxy headers off, and
  none of them, nor the Compose file, sets the forwarded-IPs setting or the Cloud Run service variable (D-24). The
  forbidden spellings are built from parts, so this file never holds them.
- Secrets and deploy records stay out of git, the image and the Cloud Build upload (D-08, D-11).
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
LAUNCHERS = {"Dockerfile": DOCKERFILE, "Makefile": ROOT / "Makefile", "tests/web/conftest.py": ROOT / "tests" / "web" / "conftest.py"}
COMPOSE = ROOT / "docker-compose.yml"

NO_PROXY = "--no-" + "proxy-headers"
BARE_PROXY = re.compile(r"(?<![\w-])--" + "proxy-headers")  # the bare flag, never the --no- form
FORBIDDEN = ("--forwarded-" + "allow-ips", "FORWARDED_" + "ALLOW_IPS", "K_" + "SERVICE")


def instructions() -> list[tuple[int, str, str]]:
    """(line number, INSTRUCTION, arguments) of the Dockerfile, comments dropped and continuations joined."""
    out, buf, start = [], "", 0
    for n, line in enumerate(DOCKERFILE.read_text(encoding="utf-8").splitlines(), 1):
        if not buf and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if not buf:
            start = n
        buf += line.rstrip("\\").strip() + " "
        if not line.rstrip().endswith("\\"):
            word, _, rest = buf.strip().partition(" ")
            out.append((start, word.upper(), rest.strip()))
            buf = ""
    return out


def stages() -> list[list[tuple[int, str, str]]]:
    """The instructions split per FROM."""
    out: list[list] = []
    for ins in instructions():
        if ins[1] == "FROM":
            out.append([])
        out[-1].append(ins)
    return out


def copies(stage) -> list[tuple[int, str | None, list[str]]]:
    """(line, --from value, sources) of each COPY in a stage."""
    found = []
    for n, word, rest in stage:
        if word != "COPY":
            continue
        parts = rest.split()
        frm = next((p.split("=", 1)[1] for p in parts if p.startswith("--from=")), None)
        args = [p for p in parts if not p.startswith("--")]
        found.append((n, frm, args[:-1]))
    return found


# --- the image ------------------------------------------------------------------------------------------------------

def test_the_image_has_a_web_stage_and_a_python_runtime():
    first, runtime = stages()
    assert first[0][2] == "node:24-slim AS web", first[0]
    assert runtime[0][2] == "python:3.12-slim", runtime[0]
    assert [rest for _, w, rest in first if w == "RUN"] == ["npm ci --no-audit --no-fund", "npm run build"]


def test_the_runtime_copies_exactly_the_app_agent_reference_data_and_the_web_build():
    _, runtime = stages()
    local = sorted(src for _, frm, srcs in copies(runtime) if frm is None for src in srcs)
    assert local == sorted(["requirements.lock", "engine", "data", "api", "agent", "reference"]), local
    built = [(frm, srcs) for _, frm, srcs in copies(runtime) if frm is not None]
    assert built == [("web", ["/web/dist"])], built
    assert ("COPY", "--from=web /web/dist web/dist") in [(w, r) for _, w, r in runtime]
    assert ("COPY", "reference reference") in [(w, r) for _, w, r in runtime]


def test_the_agent_package_ships_right_after_the_api():
    """Phase 9 (09-24 deploy checklist item 1): the service and the baseline briefs job import agent/."""
    _, runtime = stages()
    steps = [(w, r) for _, w, r in runtime]
    assert ("COPY", "agent agent") in steps
    assert steps[steps.index(("COPY", "api api")) + 1] == ("COPY", "agent agent")


def test_no_copy_ships_the_tests_deploy_or_the_whole_context():
    for stage in stages():
        for n, _, srcs in copies(stage):
            for src in srcs:
                head = src.strip("./").split("/")[0]
                assert src not in (".", "./") and head not in ("tests", "deploy"), f"line {n}: {src}"


def test_packages_come_only_from_the_locks():
    runs = [(n, rest) for n, w, rest in instructions() if w == "RUN"]
    installs = [(n, r) for n, r in runs if re.search(r"\b(pip|npm|uv)\b", r)]
    assert [r for _, r in installs] == ["npm ci --no-audit --no-fund", "npm run build",
                                        "pip install --no-cache-dir -r requirements.lock"], installs


def test_a_non_root_user_precedes_the_command():
    _, runtime = stages()
    users = [(n, rest) for n, w, rest in runtime if w == "USER"]
    cmd = [n for n, w, _ in runtime if w == "CMD"]
    assert users and cmd, (users, cmd)
    n, user = users[-1]
    assert n < cmd[-1], f"line {n}: USER after CMD"
    assert user.split(":")[0] not in ("root", "0") and user == "10001", f"line {n}: USER {user}"
    assert any(w == "RUN" and "useradd" in r and "10001" in r and "nologin" in r for _, w, r in runtime)


def test_the_command_starts_uvicorn_on_port_and_never_builds_or_migrates():
    entry = [(n, w, r) for n, w, r in instructions() if w in ("CMD", "ENTRYPOINT")]
    assert [w for _, w, _ in entry] == ["CMD"], entry
    n, _, cmd = entry[0]
    assert cmd.startswith("["), f"line {n}: CMD is not in exec form"
    assert "exec uvicorn api.app:app" in cmd and '${PORT:-8080}' in cmd and NO_PROXY in cmd, f"line {n}: {cmd}"
    for word in ("api.sandbox", "migrate", "build_template", "refdata"):
        assert word not in cmd, f"line {n}: {word} in {cmd}"


def test_the_app_never_builds_a_template_or_migrates_at_startup():
    """OPS-05: the template build is a one-off deploy job (and Compose's init), never the app or its lifespan."""
    text = (ROOT / "api" / "app.py").read_text(encoding="utf-8")
    for pattern in (r"\bbuild_template\s*\(", r"\bmigrate\s*\(", r"\brefdata\.install\s*\("):
        hits = [f"{n}: {line.strip()}" for n, line in enumerate(text.splitlines(), 1) if re.search(pattern, line)]
        assert hits == [], hits


# --- the uvicorn launchers (D-24) -----------------------------------------------------------------------------------

def uvicorn_starts(name: str) -> list[tuple[int, str]]:
    """The lines of a launcher file that start uvicorn (comments skipped)."""
    out = []
    for n, line in enumerate(LAUNCHERS[name].read_text(encoding="utf-8").splitlines(), 1):
        code = line.strip()
        if code.startswith("#"):
            continue
        if re.search(r"uvicorn\b[\"',\s]+api\.app:app", code):
            out.append((n, code))
    return out


@pytest.mark.parametrize("name", sorted(LAUNCHERS))
def test_every_uvicorn_start_turns_proxy_headers_off(name):
    starts = uvicorn_starts(name)
    assert starts, f"{name}: no uvicorn start found"
    for n, code in starts:
        assert NO_PROXY in code, f"{name}:{n}: {code}"


@pytest.mark.parametrize("path", [*LAUNCHERS.values(), COMPOSE], ids=lambda p: p.name)
def test_no_launcher_or_compose_trusts_forwarded_headers(path):
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        assert not BARE_PROXY.search(line), f"{path.name}:{n}: {line.strip()}"
        for word in FORBIDDEN:
            assert word not in line, f"{path.name}:{n}: {line.strip()}"


# --- the ignore files (D-08, D-11) ----------------------------------------------------------------------------------

def ignored(path: str) -> bool:
    r = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", path], capture_output=True)
    assert r.returncode in (0, 1), r.stderr.decode()
    return r.returncode == 0


@pytest.mark.parametrize("path", ["deploy/.env.local", ".env", ".env.local", "api/.env.prod"])
def test_git_ignores_the_configuration_and_every_env_file(path):
    assert ignored(path), path


@pytest.mark.parametrize("path", ["deploy/.env.example", ".env.example", "deploy/releases/x.json"])
def test_git_keeps_the_examples_and_the_release_records(path):
    assert not ignored(path), path


def entries(name: str) -> set[str]:
    p = ROOT / name
    assert p.is_file(), f"{name} is missing"
    return {s for s in (line.strip() for line in p.read_text(encoding="utf-8").splitlines()) if s and not s.startswith("#")}


@pytest.mark.parametrize("name", [".dockerignore", ".gcloudignore"])
def test_the_image_and_upload_ignore_secrets_and_deploy_records(name):
    found = entries(name)
    for need in (".env", ".env.*", ".git", ".venv"):
        assert need in found, f"{name} lacks {need}"
    assert found & {"deploy/.env.local", "deploy/", "deploy"}, f"{name} lacks deploy/.env.local"
    assert found & {"deploy/releases/", "deploy/releases", "deploy/", "deploy"}, f"{name} lacks deploy/releases/"


def test_the_upload_ignores_at_least_what_the_image_ignores():
    missing = entries(".dockerignore") - entries(".gcloudignore")
    assert missing == set(), missing
    assert ".gcloudignore" in entries(".gcloudignore")


def test_markdown_is_never_excluded_by_pattern():
    """data/reference/assumptions.md ships with the reference data."""
    for name in (".dockerignore", ".gcloudignore"):
        assert not [e for e in entries(name) if e.endswith(".md")], name
