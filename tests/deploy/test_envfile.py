"""deploy/envfile.py: one validated deploy configuration, keys only in the repository (06-CONTEXT D-03, D-04, D-07,
D-08; OPS-08, OPS-02).

Every filled configuration here is written under pytest's tmp_path by tests.deploy.synthetic_env from
deploy/.env.example, with obviously synthetic values. No test opens deploy/.env.local: the CLI's default path is
checked by name and by pointing envfile.LOCAL at a temporary file.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys

import pytest

from deploy import envfile
from tests.deploy import synthetic_env
from tests.deploy.synthetic_env import SYNTHETIC

ROOT = pathlib.Path(__file__).resolve().parents[2]
KEYS = ["PROJECT", "REGION", "GEMINI_MODEL", "VERTEX_LOCATION", "BILLING_ACCOUNT", "CONTACT_EMAIL", "MIN_INSTANCES",
        "GLOBAL_AI_CAP", "BUDGET_NAME", "BUDGET_AMOUNT_USD", "AR_REPO", "IMAGE_NAME", "SERVICE", "BUILD_JOB",
        "BENCH_JOB", "EARLY_BENCH_JOB", "SQL_INSTANCE", "DB_NAME", "RUN_SA_EMAIL", "DEPLOY_SA_EMAIL", "PASSCODE_SECRET",
        "HQ_PASSCODE_SECRET", "DATA_BUCKET", "UPLOADS_BUCKET", "RELEASE_DATA_DIR"]
DECIDED = {"REGION": "asia-southeast1", "GEMINI_MODEL": "gemini-3.5-flash", "VERTEX_LOCATION": "asia-southeast1",
           "MIN_INSTANCES": "0", "GLOBAL_AI_CAP": "300",  # the public values D-03/D-07 allow in the example
           "RELEASE_DATA_DIR": ""}  # the non-identifying key the example leaves empty (a local folder; 261017-v60)
PLACEHOLDER = re.compile(r"<[^<>]+>")


def filled(tmp_path, name="filled", **overrides) -> pathlib.Path:
    return synthetic_env.write(tmp_path / name, **overrides)


def line_of(path: pathlib.Path, key: str, nth: int = 1) -> int:
    """The 1-based line number of the nth `KEY=` line in the file."""
    hits = [i for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if line.startswith(key + "=")]
    return hits[nth - 1]


def refusal(path: pathlib.Path) -> str:
    with pytest.raises(envfile.ConfigError) as refused:
        envfile.load(path)
    return str(refused.value)


# --- the committed example ----------------------------------------------------------------------------------------

def test_the_example_loads_with_its_placeholders_and_holds_exactly_the_rules_keys():
    values = envfile.load(envfile.EXAMPLE, envfile.EXAMPLE, allow_placeholders=True)
    assert list(values) == KEYS == list(envfile.RULES)
    assert len(values) == 25
    for key in envfile.IDENTIFYING:
        assert PLACEHOLDER.fullmatch(values[key]), key
    assert {k: values[k] for k in DECIDED} == DECIDED


def test_identifying_keys_are_every_key_but_the_decided_public_ones():
    assert envfile.IDENTIFYING == frozenset(KEYS) - frozenset(DECIDED)
    assert envfile.OPTIONAL == frozenset({"CONTACT_EMAIL", "RELEASE_DATA_DIR"})
    assert set(SYNTHETIC) == envfile.IDENTIFYING


def test_every_key_in_the_example_has_a_one_line_explanation_above_it():
    lines = envfile.EXAMPLE.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^[A-Z][A-Z0-9_]*=", line):
            assert lines[i - 1].startswith("# ") and len(lines[i - 1]) > 10, line.split("=")[0]


def test_the_example_and_the_synthetic_values_hold_nothing_real():
    """OPS-08 prohibition: no project ID, billing account, email, service-account email or budget amount in the
    committed example; the synthetic values are fake on their face."""
    text = envfile.EXAMPLE.read_text(encoding="utf-8")
    assert not re.search(r"[A-Za-z0-9._+-]+@[A-Za-z0-9-]+\.[A-Za-z]", text)  # no address (templates use <...>@<...>)
    assert not re.search(r"[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}", text)  # no billing account
    assert not re.search(r"(US)?\$\s*[0-9]|[0-9]\s*USD\b", text)  # no budget amount
    assert SYNTHETIC["PROJECT"].startswith("x-demo") and set(SYNTHETIC["BILLING_ACCOUNT"]) <= {"0", "-"}
    for key in ("RUN_SA_EMAIL", "DEPLOY_SA_EMAIL"):
        assert SYNTHETIC[key].endswith("@x-demo-12.iam.gserviceaccount.com")


def test_the_example_alone_is_refused_for_its_placeholders():
    message = refusal(envfile.EXAMPLE)
    for key in envfile.IDENTIFYING:
        assert f"{key} (line {line_of(envfile.EXAMPLE, key)}): still holds a <placeholder>" in message


# --- a filled copy -----------------------------------------------------------------------------------------------

def test_a_synthetic_copy_loads_with_the_synthetic_and_decided_values(tmp_path):
    values = envfile.load(filled(tmp_path))
    assert values == {k: SYNTHETIC.get(k, DECIDED.get(k)) for k in KEYS}
    assert list(values) == KEYS


def test_the_synthetic_cli_writes_a_configuration_check_accepts(tmp_path):
    out = tmp_path / "cli"
    done = subprocess.run([sys.executable, "-m", "tests.deploy.synthetic_env", str(out)], cwd=ROOT,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert envfile.load(out) == envfile.load(filled(tmp_path))


def test_check_prints_ok_and_the_key_count_only(tmp_path, capsys):
    assert envfile.main(["check", "--env-file", str(filled(tmp_path))]) == 0
    assert capsys.readouterr() == ("ok: 25 keys\n", "")


# --- refusals: each names the key and the line, never the value ----------------------------------------------------

def test_a_missing_key_is_refused(tmp_path):
    path = filled(tmp_path)
    path.write_text("".join(x for x in path.read_text().splitlines(True) if not x.startswith("SERVICE=")))
    assert refusal(path) == "SERVICE: missing (every key in deploy/.env.example is required)"


def test_an_unknown_key_is_refused(tmp_path):
    path = filled(tmp_path)
    path.write_text(path.read_text() + "EXTRA_KEY=demo-zz9\n")
    message = refusal(path)
    assert message == f"EXTRA_KEY (line {line_of(path, 'EXTRA_KEY')}): unknown key (the keys are deploy/.env.example's)"
    assert "demo-zz9" not in message


def test_a_duplicate_key_is_refused(tmp_path):
    path = filled(tmp_path)
    path.write_text(path.read_text() + "SERVICE=demo-zz9\n")
    message = refusal(path)
    assert message == (f"SERVICE (line {line_of(path, 'SERVICE', 2)}): duplicate key "
                       f"(first on line {line_of(path, 'SERVICE')})")
    assert "demo-zz9" not in message


def test_an_empty_value_is_refused(tmp_path):
    path = filled(tmp_path, SERVICE="")
    assert refusal(path) == f"SERVICE (line {line_of(path, 'SERVICE')}): empty"


def test_only_contact_email_and_release_data_dir_may_be_empty(tmp_path):
    assert envfile.load(filled(tmp_path, CONTACT_EMAIL=""))["CONTACT_EMAIL"] == ""
    assert envfile.load(filled(tmp_path, "no-data-dir", RELEASE_DATA_DIR=""))["RELEASE_DATA_DIR"] == ""
    for key in envfile.RULES:
        if key not in envfile.OPTIONAL:
            path = filled(tmp_path, key.lower(), **{key: ""})
            assert refusal(path) == f"{key} (line {line_of(path, key)}): empty"


def test_a_value_still_holding_a_placeholder_is_refused(tmp_path):
    path = filled(tmp_path, SERVICE="<cloud-run-service>", CONTACT_EMAIL="<contact-email-or-empty>")
    message = refusal(path)
    assert message.splitlines() == [
        f"CONTACT_EMAIL (line {line_of(path, 'CONTACT_EMAIL')}): still holds a <placeholder>; fill in the real value",
        f"SERVICE (line {line_of(path, 'SERVICE')}): still holds a <placeholder>; fill in the real value"]
    assert "cloud-run-service" not in message and "contact-email" not in message


@pytest.mark.parametrize("line", ['SERVICE="demo-zz9"', "SERVICE='demo-zz9'", 'SERVICE=demo-"zz9'])
def test_a_quoted_value_is_refused(tmp_path, line):
    path = filled(tmp_path)
    path.write_text(re.sub(r"^SERVICE=.*$", line, path.read_text(), flags=re.M))
    message = refusal(path)
    assert message == (f"SERVICE (line {line_of(path, 'SERVICE')}): a quoted value; write SERVICE=value without "
                       f"quotes")
    assert "zz9" not in message


@pytest.mark.parametrize("line", ["export SERVICE=demo-zz9", "SERVICE = demo-zz9", " SERVICE=demo-zz9"])
def test_a_line_that_is_not_plain_key_value_names_its_key(tmp_path, line):
    path = filled(tmp_path)
    text = re.sub(r"^SERVICE=.*$", line, path.read_text(), flags=re.M)
    path.write_text(text)
    n = text.splitlines().index(line) + 1
    message = refusal(path)
    assert message == f"SERVICE (line {n}): not a plain KEY=value line (no export, no spaces, no quotes)"
    assert "zz9" not in message


def test_a_line_with_no_key_names_only_its_line(tmp_path):
    path = filled(tmp_path)
    path.write_text(path.read_text() + "demo-zz9\n")
    n = len(path.read_text().splitlines())
    assert refusal(path) == f"line {n}: not a comment, a blank line or KEY=value"


def test_every_problem_is_reported_at_once(tmp_path):
    path = filled(tmp_path, MIN_INSTANCES="2", DB_NAME="")
    path.write_text("".join(x for x in path.read_text().splitlines(True) if not x.startswith("SERVICE=")))
    assert refusal(path).splitlines() == [
        f"MIN_INSTANCES (line {line_of(path, 'MIN_INSTANCES')}): must be 0 or 1",
        f"DB_NAME (line {line_of(path, 'DB_NAME')}): empty",
        "SERVICE: missing (every key in deploy/.env.example is required)"]


# --- per-key rules (OPS-02 boundary and precision) ------------------------------------------------------------------

def rule_refusal(tmp_path, key, value) -> tuple[str, str]:
    """(the refusal, the exact message the key's rule should give)."""
    path = filled(tmp_path, f"{key.lower()}-{abs(hash(value))}", **{key: value})
    return refusal(path), f"{key} (line {line_of(path, key)}): must be {envfile.RULES[key][1]}"


@pytest.mark.parametrize("key,good,bad", [
    ("MIN_INSTANCES", ["0", "1"], ["2", "-1", "01", "1.0", " 1", "1 "]),
    ("GLOBAL_AI_CAP", ["300", "1", "9999999"], ["0", "-3", "3e2", "300.0", "0300", "+300", "10000000"]),
    ("BUDGET_AMOUNT_USD", ["12", "45.50", "7.5"], ["0", "-5", "12USD", "1.234", "05", "12.", ".5", "1e3"]),
    ("PROJECT", ["x-demo-12", "abcdef"], ["X-demo-12", "1demo-12", "demo", "x-demo-12-", "x_demo_12"]),
    ("REGION", ["asia-southeast1", "us-central1"], ["asia", "Asia-southeast1", "asia-southeast"]),
    ("VERTEX_LOCATION", ["asia-southeast1", "global", "us", "eu"], ["Global", "asia", "europe"]),
    ("GEMINI_MODEL", ["gemini-3.5-flash", "gemini-3.8-flash"], ["gpt-4", "gemini-", "gemini-x$(id)", "gemini-a b"]),
    ("BILLING_ACCOUNT", ["000000-000000-000000", "0A1B2C-3D4E5F-60789A"], ["000000000000000000", "0a1b2c-3d4e5f-60789a",
                                                                          "000000-000000-00000"]),
    ("CONTACT_EMAIL", ["judge+demo@example.com", "a.b@mail.example.org"], ["not an address", "a@b", "a;b@example.com",
                                                                            "a@example.com>x"]),
    ("SERVICE", ["demo-svc", "ab"], ["a", "Demo-svc", "demo_svc", "demo-svc-", "demo;svc", "x" * 51]),
    ("HQ_PASSCODE_SECRET", ["demo-hq-pass", "ab"], ["a", "Demo-hq-pass", "demo_hq_pass", "demo-hq-pass-", "demo;hq",
                                                    "x" * 51]),
    ("DB_NAME", ["demo_db", "_x"], ["Demo_db", "1db", "demo-db", "x" * 64]),
    ("DATA_BUCKET", ["demo-data", "abc", "x" * 63], ["ab", "Demo-data", "demo_data", "-demo", "demo-", "demo.data",
                                                     "x" * 64]),
    ("UPLOADS_BUCKET", ["demo-uploads", "abc", "x" * 63], ["ab", "Demo-uploads", "demo_uploads", "-demo", "demo-",
                                                           "demo.uploads", "x" * 64]),
    ("RELEASE_DATA_DIR", ["", "/tmp/public-set/out", "../ShelfShift-web/public-data/out",
                          "/Users/someone/AI Builder/out"], ["~/out", " /out", "/out ", "/a  b", "/o;ut", "/o$(id)",
                                                             "/o*"]),
])
def test_each_key_accepts_its_rule_and_nothing_else(tmp_path, key, good, bad):
    for value in good:
        # a new PROJECT takes service accounts of that project with it
        accounts = {k: f"{k[:3].lower()}-sa@{value}.iam.gserviceaccount.com"
                    for k in ("RUN_SA_EMAIL", "DEPLOY_SA_EMAIL")}
        extra = accounts if key == "PROJECT" else {}
        path = filled(tmp_path, f"{key.lower()}-ok-{abs(hash(value))}", **{key: value, **extra})
        assert envfile.load(path)[key] == value
    for value in bad:
        got, want = rule_refusal(tmp_path, key, value)
        assert got == want, value


@pytest.mark.parametrize("key", ["RUN_SA_EMAIL", "DEPLOY_SA_EMAIL"])
def test_a_service_account_must_belong_to_the_configured_project(tmp_path, key):
    assert envfile.RULES[key][1] == "<name>@<PROJECT>.iam.gserviceaccount.com for the configured PROJECT"
    for value in ("sa-zz99@x-other-34.iam.gserviceaccount.com", "sa-zz99@x-demo-12.iam.example.com",
                  "sa@x-demo-12.iam.gserviceaccount.com", "sa-zz99@x-demo-123.iam.gserviceaccount.com"):
        got, want = rule_refusal(tmp_path, key, value)
        assert got == want, value
        assert "x-demo-12" not in got and "x-other-34" not in got
    # the same address under another PROJECT is refused, whichever line comes first
    peer = ({"RUN_SA_EMAIL", "DEPLOY_SA_EMAIL"} - {key}).pop()
    other = filled(tmp_path, "other-project", PROJECT="x-other-34",
                   **{peer: "peer-sa@x-other-34.iam.gserviceaccount.com"})
    assert refusal(other) == f"{key} (line {line_of(other, key)}): must be {envfile.RULES[key][1]}"
    text = other.read_text().splitlines(True)
    first = [x for x in text if x.startswith(key + "=")] + [x for x in text if not x.startswith(key + "=")]
    other.write_text("".join(first))  # the service account above PROJECT
    assert refusal(other) == f"{key} (line 1): must be {envfile.RULES[key][1]}"


def test_iam_db_user_drops_the_gserviceaccount_suffix():
    assert envfile.iam_db_user("run@x-demo-12.iam.gserviceaccount.com") == "run@x-demo-12.iam"
    with pytest.raises(envfile.ConfigError):
        envfile.iam_db_user("run@x-demo-12.iam.example.com")


def test_the_hq_secret_must_be_another_secret_than_the_passcode_secret(tmp_path, capsys):
    """06-REVIEW IN-05: one secret for both credentials would make the HQ passcode the store passcode."""
    path = filled(tmp_path, HQ_PASSCODE_SECRET=SYNTHETIC["PASSCODE_SECRET"])
    message = refusal(path)
    assert message == (f"HQ_PASSCODE_SECRET (line {line_of(path, 'HQ_PASSCODE_SECRET')}): must name another secret "
                       f"than PASSCODE_SECRET")
    assert SYNTHETIC["PASSCODE_SECRET"] not in message
    assert envfile.main(["check", "--env-file", str(path)]) == 2
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("config error: HQ_PASSCODE_SECRET") and SYNTHETIC["PASSCODE_SECRET"] not in err


def test_different_passcode_secrets_load(tmp_path):
    values = envfile.load(filled(tmp_path))
    assert values["PASSCODE_SECRET"] != values["HQ_PASSCODE_SECRET"]


# --- the CLI ------------------------------------------------------------------------------------------------------

def test_a_refused_configuration_prints_one_config_error_line_per_problem_and_exits_2(tmp_path, capsys):
    path = filled(tmp_path, SERVICE="demo;zz9", MIN_INSTANCES="7")
    assert envfile.main(["export", "--env-file", str(path)]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.splitlines() == [f"config error: MIN_INSTANCES (line {line_of(path, 'MIN_INSTANCES')}): must be 0 or 1",
                                f"config error: SERVICE (line {line_of(path, 'SERVICE')}): must be "
                                f"{envfile.RULES['SERVICE'][1]}"]
    assert "zz9" not in err


def test_a_missing_file_is_a_config_error(tmp_path, capsys):
    assert envfile.main(["check", "--env-file", str(tmp_path / "absent")]) == 2
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("config error: ") and "absent" in err and "deploy/.env.example" in err


def test_export_prints_one_shell_quoted_line_per_key_in_the_example_order(tmp_path, capsys):
    path = filled(tmp_path, CONTACT_EMAIL="judge+demo@example.com")
    assert envfile.main(["export", "--env-file", str(path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split("=", 1)[0] for line in lines] == [f"export {k}" for k in KEYS]
    assert "export CONTACT_EMAIL=judge+demo@example.com" in lines


@pytest.mark.skipif(not pathlib.Path("/bin/bash").exists(), reason="needs /bin/bash")
@pytest.mark.parametrize("contact", ["", "judge+demo@example.com"])
def test_bash_evaluates_export_to_exactly_the_loaded_values(tmp_path, contact):
    """macOS /bin/bash is 3.2 (06-RESEARCH Pitfall 12): eval of the export output gives back every loaded value."""
    path = filled(tmp_path, CONTACT_EMAIL=contact)
    script = ('eval "$("$PY" -m deploy.envfile export --env-file "$F")" || exit 9; '
              'for k in ' + " ".join(KEYS) + '; do printf "%s=%s\\n" "$k" "${!k}"; done')
    done = subprocess.run(["/bin/bash", "-c", script], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "PY": sys.executable, "F": str(path)})
    assert done.returncode == 0, done.stderr
    got = dict(line.split("=", 1) for line in done.stdout.splitlines())
    assert got == envfile.load(path)


def test_the_default_path_is_the_git_ignored_local_file(tmp_path, monkeypatch, capsys):
    assert envfile.LOCAL == ROOT / "deploy" / ".env.local"
    assert envfile.EXAMPLE == ROOT / "deploy" / ".env.example"
    if shutil.which("git"):
        done = subprocess.run(["git", "check-ignore", "-q", "deploy/.env.local"], cwd=ROOT)
        assert done.returncode == 0  # git ignores it (06-01's .gitignore line)
    monkeypatch.setattr(envfile, "LOCAL", filled(tmp_path))  # the default is read at call time; never the real file
    assert envfile.main(["check"]) == 0
    assert capsys.readouterr().out == "ok: 25 keys\n"
