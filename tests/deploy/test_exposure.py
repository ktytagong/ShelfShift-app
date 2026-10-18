"""No project ID in the code, and the push and release gate that keeps it out (06-CONTEXT D-07, D-11; research
Pitfall 11; the no-exposure rule).

Every git-tracked file of this repository (deploy/releases/ excluded: the release records name the project by design,
D-11) is scanned for the known project IDs, and a hit is reported as path:line, never as the ID. The IDs come from two
places only, so no file here has to spell one:

- the earlier personal project's ID, STALE_PROJECT_ID, imported from tests/data/test_honesty.py (never respelled);
- the demo project's ID, read in gate mode from the PROJECT key of the deploy configuration that SS_DEPLOY_ENV_FILE
  names, loaded and checked by deploy/envfile.py.

Plain mode (SS_DEPLOY_ENV_FILE not in the environment, as in `make test`) scans for STALE_PROJECT_ID only and reads
no other variable. Gate mode (SS_DEPLOY_ENV_FILE in the environment, even empty) is what `make deploy-check` (the
user's push gate) and deploy/release.py (before any cloud command, 06-07) run. A gate run that cannot load a real
PROJECT fails with a message starting "deploy gate refused:"; it never skips, because a skipped gate would let a
push or a release through without the scan.

The deploy-facing files (deploy/ except releases, both READMEs, the Dockerfile, the Compose file, the ignore files,
api/bench_input.json and tests/deployed) also hold no project-ID literal, no Thai currency and, when the denylist is
set, no retailer name. The README and runbook rules pin what OPS-01 and OPS-08 ask those two files to say.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

import pytest

from deploy import envfile
from tests.data.test_honesty import (  # imported, never respelled
    PROJECT_ID_LITERAL, PUBLIC_MARK, STALE_PROJECT_ID, THAI_CURRENCY, denylist,
)
from tests.deploy import synthetic_env

ROOT = pathlib.Path(__file__).resolve().parents[2]  # the ShelfShift repo
GATE_ENV = "SS_DEPLOY_ENV_FILE"
REFUSED = "deploy gate refused:"
RELEASES = "deploy/releases/"
SYNTHETIC_PROJECT = synthetic_env.SYNTHETIC["PROJECT"]


class GateError(AssertionError):
    """The gate could not load a real PROJECT; the message starts with REFUSED and never holds a value."""


def configured_project(value: str) -> str:
    """PROJECT of the configuration file `value` names (a relative path is taken from the repository root), loaded
    and checked by deploy.envfile.load. An empty value, an absent or unreadable file, and every refusal of the
    loader (PROJECT missing, a placeholder or invalid included) raise GateError with the loader's key-and-line text."""
    if not value.strip():
        raise GateError(f"{REFUSED} {GATE_ENV} is empty; point it at the filled deploy configuration")
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    try:
        return envfile.load(path)["PROJECT"]
    except envfile.ConfigError as exc:
        raise GateError(f"{REFUSED} " + "; ".join(str(exc).splitlines())) from None


def tracked(root: pathlib.Path | str = ROOT) -> list[str]:
    """The repository-relative paths git tracks under `root` (git ls-files, so ignored files are never read)."""
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [name for name in out.decode("utf-8").split("\0") if name]


def known_ids() -> list[str]:
    """STALE_PROJECT_ID, plus the configured PROJECT in gate mode (GateError when it cannot be loaded)."""
    ids = [STALE_PROJECT_ID]
    if GATE_ENV in os.environ:
        ids.append(configured_project(os.environ[GATE_ENV]))
    return ids


def id_hits(ids: list[str], root: pathlib.Path | str = ROOT) -> list[str]:
    """'path:line' for each line of a tracked text file holding one of `ids`; deploy/releases/ and files with a NUL
    byte are skipped. An ID inside a path is shown as <id>, so the result never holds one."""
    ids = [i for i in ids if i]
    root = pathlib.Path(root)
    found = []
    for name in tracked(root):
        if name.startswith(RELEASES):
            continue
        path = root / name
        if not path.is_file():  # deleted in the working tree, or a submodule
            continue
        raw = path.read_bytes()
        if b"\0" in raw:
            continue
        shown = name
        for i in ids:
            shown = shown.replace(i, "<id>")
        for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
            if any(i in line for i in ids):
                found.append(f"{shown}:{number}")
    return found


def git_repo(root: pathlib.Path, files: dict[str, bytes | str]) -> pathlib.Path:
    """A throwaway repository at `root` with `files` written and committed (no hooks, no signing, no identity)."""
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "--", *files], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=selftest", "-c", "user.email=", "-c",
                    "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "selftest"],
                   check=True)
    return root


# --- the gate: plain mode and gate mode ---------------------------------------------------------------------------

def test_plain_mode_knows_only_the_stale_id_and_reads_no_other_variable(monkeypatch):
    monkeypatch.delenv(GATE_ENV, raising=False)
    monkeypatch.setenv("PROJECT", "x-demo-77")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "x-demo-78")
    assert known_ids() == [STALE_PROJECT_ID]


def test_gate_mode_adds_the_configured_project(tmp_path, monkeypatch):
    monkeypatch.setenv(GATE_ENV, str(synthetic_env.write(tmp_path / "gate.env")))
    assert known_ids() == [STALE_PROJECT_ID, SYNTHETIC_PROJECT]


def test_a_relative_gate_path_is_taken_from_the_repository_root(tmp_path, monkeypatch):
    repo, elsewhere = tmp_path / "repo", tmp_path / "elsewhere"
    elsewhere.mkdir()
    (repo / "conf").mkdir(parents=True)
    synthetic_env.write(repo / "conf" / "gate.env")
    monkeypatch.setitem(globals(), "ROOT", repo)  # configured_project reads the module's ROOT at call time
    monkeypatch.chdir(elsewhere)
    assert configured_project("conf/gate.env") == SYNTHETIC_PROJECT


def _without(path: pathlib.Path, key: str) -> pathlib.Path:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith(f"{key}=")]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("case", ["example", "absent", "empty", "blank", "directory", "placeholder", "invalid",
                                  "missing"])
def test_the_gate_refuses_without_a_real_project(case, tmp_path, monkeypatch):
    value = {
        "example": "deploy/.env.example",
        "absent": str(tmp_path / "absent.env"),
        "empty": "",
        "blank": "   ",
        "directory": str(tmp_path),
        "placeholder": lambda: str(synthetic_env.write(tmp_path / "p.env", PROJECT="<demo-project-id>")),
        "invalid": lambda: str(synthetic_env.write(tmp_path / "i.env", PROJECT="Bad_Project_9")),
        "missing": lambda: str(_without(synthetic_env.write(tmp_path / "m.env"), "PROJECT")),
    }[case]
    value = value() if callable(value) else value
    monkeypatch.chdir(tmp_path)  # a relative value is taken from the repository root, not the cwd
    monkeypatch.setenv(GATE_ENV, value)
    with pytest.raises(GateError) as caught:
        known_ids()
    message = str(caught.value)
    assert message.startswith(REFUSED)
    assert "Bad_Project_9" not in message and SYNTHETIC_PROJECT not in message
    if case in ("placeholder", "invalid", "missing"):
        assert "PROJECT" in message  # the loader's key-and-line text


def test_a_gate_refusal_fails_the_scan_and_never_skips(tmp_path, monkeypatch):
    monkeypatch.setenv(GATE_ENV, str(tmp_path / "absent.env"))
    with pytest.raises(pytest.fail.Exception) as caught:
        test_no_known_project_id_in_tracked_files()
    assert str(caught.value).startswith(REFUSED)


# --- the tracked-file scan and its self-test ---------------------------------------------------------------------

def test_id_hits_finds_a_planted_id_and_skips_releases_binaries_and_untracked_files(tmp_path):
    repo = git_repo(tmp_path / "repo", {
        "a.txt": "first line\nx-demo-99 here\n",
        "deploy/releases/r.json": '{"project": "x-demo-99"}\n',
        "blob.bin": b"x-demo-99\0binary",
        "clean.txt": "nothing to see\n",
    })
    (repo / "untracked.txt").write_text("x-demo-99\n", encoding="utf-8")
    assert id_hits(["x-demo-99"], repo) == ["a.txt:2"]


def test_an_id_inside_a_path_is_never_shown(tmp_path):
    repo = git_repo(tmp_path / "repo", {"notes/x-demo-99.txt": "x-demo-99\n"})
    found = id_hits(["x-demo-99"], repo)
    assert found == ["notes/<id>.txt:1"] and not any("x-demo-99" in hit for hit in found)


def test_gate_mode_scans_for_the_configured_project(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo", {"a.txt": "ok\n", "b.txt": f"one\ntwo {SYNTHETIC_PROJECT}\n"})
    monkeypatch.delenv(GATE_ENV, raising=False)
    assert id_hits(known_ids(), repo) == []
    monkeypatch.setenv(GATE_ENV, str(synthetic_env.write(tmp_path / "gate.env")))
    assert id_hits(known_ids(), repo) == ["b.txt:2"]


def test_no_known_project_id_in_tracked_files():
    try:
        found = id_hits(known_ids())
    except GateError as exc:
        pytest.fail(str(exc), pytrace=False)
    if found:
        pytest.fail("a known project ID is in tracked files (deploy/releases/ excluded) at: " + ", ".join(found),
                    pytrace=False)


# --- the deploy-facing text rules ----------------------------------------------------------------------------------

# deploy/provision.py calls the gcloud command group `projects`, whose quoted verb would read as a project literal.
GCLOUD_PROJECTS_GROUP = re.compile(r"""gcloud\(\s*(?:["']billing["']\s*,\s*)?["']projects["']\s*,""")
DEPLOY_FACING = ("README.md", "deploy/README.md", "Dockerfile", "docker-compose.yml", ".dockerignore",
                 ".gcloudignore", "api/bench_input.json")


def deploy_facing() -> list[tuple[pathlib.Path, str]]:
    """(path, text) of the deploy-facing files: tracked files under deploy/ (releases excluded) and tests/deployed,
    plus the named files when present. tests/deploy is left out: its helpers hold synthetic values on purpose."""
    names = {n for n in tracked() if (n.startswith("deploy/") and not n.startswith(RELEASES))
             or n.startswith("tests/deployed/")}
    names |= {n for n in DEPLOY_FACING if (ROOT / n).is_file()}
    files = []
    for name in sorted(names):
        path = ROOT / name
        if path.is_file() and b"\0" not in path.read_bytes():
            files.append((path, path.read_text(encoding="utf-8")))
    return files


def text_hits(pattern, files) -> list[str]:
    found = []
    for path, text in files:
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                found.append(f"{path.relative_to(ROOT)}:{number}")
    return found


def test_the_deploy_facing_walk_sees_the_scripts_and_never_releases():
    seen = {path.relative_to(ROOT).as_posix() for path, _ in deploy_facing()}
    for name in ("deploy/release.py", "deploy/setup.sh", "deploy/.env.example", "Dockerfile", "docker-compose.yml",
                 "README.md", "api/bench_input.json", "tests/deployed/test_journey_deployed.py"):
        assert name in seen, name
    assert not any(name.startswith(RELEASES) or name.startswith("tests/deploy/") for name in seen)


def test_deploy_facing_files_hold_no_project_literal():
    files = [(path, GCLOUD_PROJECTS_GROUP.sub("gcloud(", text)) for path, text in deploy_facing()]
    assert text_hits(PROJECT_ID_LITERAL, files) == []


def test_the_projects_group_exemption_still_catches_a_literal():
    line = GCLOUD_PROJECTS_GROUP.sub("gcloud(", 'gcloud("projects", "describe", project="some-demo-id")')
    assert PROJECT_ID_LITERAL.search(line)
    assert not PROJECT_ID_LITERAL.search(GCLOUD_PROJECTS_GROUP.sub(
        "gcloud(", 'gcloud("projects", "add-iam-policy-binding", project, "--member",'))
    billing = GCLOUD_PROJECTS_GROUP.sub("gcloud(", 'gcloud("billing", "projects", "describe", project="some-demo-id")')
    assert PROJECT_ID_LITERAL.search(billing)
    assert not PROJECT_ID_LITERAL.search(GCLOUD_PROJECTS_GROUP.sub(
        "gcloud(", 'gcloud("billing", "projects", "describe", project, "--format=value(billingAccountName)",'))


def test_the_retailer_is_never_named_in_deploy_files():
    found = []
    for index, name in enumerate(denylist(), 1):
        pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)
        found += [f"denylist entry {index} at {hit}" for hit in text_hits(pattern, deploy_facing())]
    assert found == []


def test_no_thai_currency_in_deploy_files():
    assert text_hits(THAI_CURRENCY, deploy_facing()) == []


# --- README.md (OPS-01) and deploy/README.md, the user's runbook (OPS-08) -------------------------------------------

README = ROOT / "README.md"
RUNBOOK = ROOT / "deploy" / "README.md"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
RUN_APP_URL = re.compile(r"\.run\.app\b", re.IGNORECASE)
THAI_WORD = re.compile(r"\bThai\b", re.IGNORECASE)
DOLLAR_AMOUNT = re.compile(r"(?:US)?\$\s?\d|\b\d[\d,.]*\s?(?:USD|US dollars|dollars)\b", re.IGNORECASE)
PASSCODE_SETTING = re.compile(r"\b(?:DEMO|SMOKE)_PASSCODE=(<[^<>\n]*>|\S*)")
KEY_REF = re.compile(r"\$\{?([A-Z][A-Z0-9_]*)")
# Spelled in pieces, as tests/deploy/test_release.py's FORBIDDEN is, so no file spells them whole.
RUNBOOK_FORBIDDEN = ("--min" + "-instances", "--max" + "-instances", "--no-cpu" + "-throttling", "--use" + "-http2",
                     "from google import " + "genai")
RUNBOOK_ORDER = ("deploy/.env.example", "bash deploy/setup.sh base", "gcloud auth print-access-token",
                 "publishers/google/models/", "VERTEX_LOCATION", "bash deploy/bench-early.sh", "BUDGET_AMOUNT_USD",
                 "bash deploy/setup.sh data", "gcloud sql connect", "bash deploy/release.sh", "make journey-deployed",
                 "deploy/releases/", "bash deploy/release.sh --rollback")


def read(path: pathlib.Path) -> str:
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def sections(text: str) -> list[str]:
    """The text split at its level-2 headings, each part starting with its heading."""
    return re.split(r"\n(?=## )", text)


def step(text: str, number: int) -> str:
    """The runbook's numbered step `number` (a '### N.' heading), up to the next heading of level 2 or 3."""
    m = re.search(rf"^### {number}\. .*?(?=^##+ |\Z)", text, re.MULTILINE | re.DOTALL)
    assert m, f"runbook step {number} is missing"
    return m.group(0)


@pytest.mark.parametrize("path", [README, RUNBOOK], ids=["README.md", "deploy/README.md"])
def test_the_readmes_name_no_value(path):
    text = read(path)
    assert EMAIL.findall(text) == []
    assert not RUN_APP_URL.search(text)
    assert not PROJECT_ID_LITERAL.search(text)
    assert not DOLLAR_AMOUNT.search(text), "a dollar amount (the budget never appears in a public artifact, D-02)"
    for value in PASSCODE_SETTING.findall(text):
        assert value == "" or re.fullmatch(r"<[^<>]+>", value), "a passcode value"
    assert not THAI_CURRENCY.search(text) and not THAI_WORD.search(text)


def test_the_readme_explains_the_local_run_and_the_tests():
    text = read(README)
    assert text.lstrip("# \n").startswith("ShelfShift")
    assert PUBLIC_MARK.rsplit(" · ", 1)[0] in text  # the mark without its scenario date (05.2 D-04)
    for needed in ("docker compose up", "make setup", "make test", "make compose-check", "make db-up", "GCP",
                   ".env", "requirements.lock", "web/package-lock.json", "](deploy/README.md)", "deploy/.env.example"):
        assert needed in text, needed
    [db_up] = [s for s in sections(text) if "make db-up" in s]
    assert "5432" in db_up and "SS_DB_PORT" in db_up and "one browser suite at a time" in db_up


def test_the_runbook_runs_the_steps_in_order():
    text = read(RUNBOOK)
    at = 0
    for marker in RUNBOOK_ORDER:
        found = text.find(marker, at)
        assert found >= 0, f"{marker!r} is missing or out of order"
        at = found + len(marker)


def test_every_key_the_runbook_names_is_a_configuration_key():
    named = set(KEY_REF.findall(read(RUNBOOK)))
    assert named, "the runbook names no configuration key"
    assert named <= set(envfile.RULES), sorted(named - set(envfile.RULES))


def test_the_runbook_spells_no_forbidden_flag_or_sdk_import():
    text = read(RUNBOOK)
    for bad in RUNBOOK_FORBIDDEN:
        assert bad not in text, bad


def test_the_model_check_is_an_sdk_free_request():
    s = step(read(RUNBOOK), 3)
    for needed in ("curl", 'Authorization: Bearer $(gcloud auth print-access-token)', "aiplatform.googleapis.com",
                   "${VERTEX_LOCATION}-aiplatform.googleapis.com", "v1beta1/publishers/google/models/${GEMINI_MODEL}",
                   "projects/${PROJECT}/locations/${VERTEX_LOCATION}", "models/${GEMINI_MODEL}:countTokens",
                   's/${PROJECT}/<PROJECT>/g', "model=ok", "model=global", "model=blocked",
                   "404", "global", "catalog evidence"):
        assert needed in s, needed


def test_no_runbook_variable_is_followed_by_a_colon_unbraced():
    """zsh reads `$GEMINI_MODEL:countTokens` as the `:c` modifier and sends `…flashountTokens` (2026-10-16)."""
    assert not re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*:[A-Za-z]", read(RUNBOOK))


def test_the_push_waits_for_the_gate():
    text = read(RUNBOOK)
    assert "make deploy-check" in text and "git push" in text
    assert text.index("make deploy-check") < text.index("git push")
    s = step(text, 9)
    flat = " ".join(s.split())  # the prose wraps
    assert "make deploy-check" in flat and "only when it passes" in flat


def test_the_passcode_stays_out_of_the_transcript_and_the_second_network_check_is_required():
    text = read(RUNBOOK)
    for needed in ("gcloud secrets versions access latest", "--new-passcode", "passcode counter"):
        assert needed in text, needed
    assert not re.search(r"(?:asks?|prompts?)(?: you)? for (?:a |the )?passcode", step(text, 6), re.IGNORECASE)
    check = step(text, 8)
    [para] = [p for p in check.split("\n\n") if "second-network" in p.lower()][:1] or [""]
    assert "required" in para.lower(), "the second-network check is not marked required"
    for answer in ("`separate`", "`shared`", "`not run`"):
        assert answer in check, answer


def test_the_review_notes_reach_the_runbook():
    text = read(RUNBOOK)
    for needed in ("public IP", "5-10 min", "3-8 min", "budget=manual", "--no-invoker-iam-check",
                   "iam.allowedPolicyMemberDomains", "--breaking-template", "reconcile", "Not run in Phase 6",
                   "pytest -m live", "2026-11-06"):
        assert needed in text, needed
