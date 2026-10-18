"""deploy/cloud.py and deploy/provision.py: provisioning written out as commands the user runs (OPS-08, OPS-07,
OPS-02; 06-CONTEXT D-02, D-04, D-05, D-24; 06-REVIEWS R1-2).

Nothing here talks to Google Cloud. Every configuration is written under pytest's tmp_path by
tests.deploy.synthetic_env (project x-demo-12, an all-zero billing account), and every command goes to a DryRunner,
which prints and records it and runs nothing. The CLI tests run with a PATH that holds no gcloud.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import subprocess
import sys

import pytest

from deploy import cloud, envfile, provision
from deploy.cloud import CommandError, DryRunner, Runner
from tests.deploy import synthetic_env
from tests.deploy.synthetic_env import SYNTHETIC

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROJECT = SYNTHETIC["PROJECT"]
BILLING = SYNTHETIC["BILLING_ACCOUNT"]
RUN_SA = SYNTHETIC["RUN_SA_EMAIL"]
DEPLOY_SA = SYNTHETIC["DEPLOY_SA_EMAIL"]
REGION = "asia-southeast1"
NO_GCLOUD_PATH = "/usr/bin:/bin"  # macOS ships no gcloud there; the CLI tests check it


@pytest.fixture
def cfg(tmp_path) -> dict[str, str]:
    return envfile.load(synthetic_env.write(tmp_path / "env"))


def g(*args: str) -> list[str]:
    return ["gcloud", *args, "--project", PROJECT]


def words(calls, n: int) -> list[str]:
    """The first n words of every recorded command, joined (the verb, for order checks)."""
    return [" ".join(c[:n]) for c in calls]


# --- cloud.py -------------------------------------------------------------------------------------------------------

def test_gcloud_appends_the_project():
    assert cloud.gcloud("sql", "instances", "list", project="x-demo-12") == [
        "gcloud", "sql", "instances", "list", "--project", "x-demo-12"]


def test_the_dry_runner_prints_and_records_each_command_and_withholds_stdin(capsys):
    r = DryRunner()
    assert r.run(["gcloud", "secrets", "create", "s", "--data-file", "-"], stdin="the-value") == ""
    assert r.ok(["gcloud", "x", "describe"]) is False
    assert r.json(["gcloud", "x", "list"]) is None
    out = capsys.readouterr().out
    assert out.splitlines() == ["+ gcloud secrets create s --data-file - < (withheld)", "+ gcloud x describe",
                                "+ gcloud x list --format=json"]
    assert r.calls == [["gcloud", "secrets", "create", "s", "--data-file", "-"], ["gcloud", "x", "describe"],
                       ["gcloud", "x", "list", "--format=json"]]
    assert r.inputs == ["(withheld)", None, None]
    assert "the-value" not in out and "the-value" not in repr(r.calls)


def test_the_dry_runner_answers_from_its_rules_and_fails_on_its_predicate(capsys):
    r = DryRunner(rules=[(lambda a: "describe" in a, True), (lambda a: "list" in a, [{"name": "n"}])],
                  fail=lambda a: "boom" in a)
    assert r.ok(["gcloud", "x", "describe"]) is True
    assert r.json(["gcloud", "x", "list"]) == [{"name": "n"}]
    with pytest.raises(CommandError) as exc:
        r.run(["gcloud", "billing", "budgets", "boom", "--billing-account", BILLING])
    assert exc.value.argv0 == "gcloud billing budgets boom" and exc.value.returncode == 1
    assert len(r.calls) == 3  # a failing command is still recorded and printed


def test_the_runner_reports_the_first_words_the_exit_code_and_the_stderr_tail_never_stdin(capsys):
    script = ("import sys; sys.stdin.read(); "
              "sys.stderr.write(''.join(f'line {i}\\n' for i in range(9))); sys.exit(3)")
    with pytest.raises(CommandError) as exc:
        Runner().run([sys.executable, "-c", script], stdin="stdin-secret-value")
    err = exc.value
    assert err.argv0 == sys.executable and err.returncode == 3
    assert err.stderr_tail.splitlines() == [f"line {i}" for i in range(4, 9)]
    assert "stdin-secret-value" not in str(err) and "stdin-secret-value" not in capsys.readouterr().out
    assert "exit 3" in str(err) and "line 8" in str(err)


def test_the_runner_keeps_the_whole_stderr_masked_for_matching_and_never_prints_it(capsys):
    """A3: CommandError.stderr holds the whole masked stderr, for matching only (an update notice can push gcloud's
    `Image not found.` out of the 5-line tail); the message and the printed output still carry only the tail."""
    script = ("import sys; "
              "sys.stderr.write(''.join(f'line {i} hidden-value-x\\n' if i == 1 else f'line {i}\\n' "
              "for i in range(9))); sys.exit(1)")
    r = Runner()
    r.hide("hidden-value-x", "(hidden)")
    with pytest.raises(CommandError) as exc:
        r.run([sys.executable, "-c", script])
    err = exc.value
    assert err.stderr.splitlines() == [f"line {i} (hidden)" if i == 1 else f"line {i}" for i in range(9)]
    assert err.stderr_tail.splitlines() == [f"line {i}" for i in range(4, 9)]
    assert str(err) == f"{err.argv0} failed (exit 1): " + err.stderr_tail
    assert "line 3" not in str(err) and "hidden-value-x" not in str(err) + err.stderr
    out = capsys.readouterr()
    assert not [line for line in (out.out + out.err).splitlines() if line.startswith("line ")]  # only the echo
    assert CommandError("gcloud x", 1, "tail only").stderr == "tail only"  # the default: the tail


def test_the_runner_ok_json_and_a_missing_program(capsys):
    r = Runner()
    assert r.ok([sys.executable, "-c", "print('x')"]) is True
    assert r.ok([sys.executable, "-c", "import sys; sys.exit(1)"]) is False
    payload = [{"displayName": "d", "amount": {"specifiedAmount": {"units": "12"}}}]
    # json() appends --format=json; this program ignores its arguments and prints the payload
    assert r.json([sys.executable, "-c", f"print({json.dumps(json.dumps(payload))})"]) == payload
    with pytest.raises(CommandError) as exc:
        r.run(["no-such-program-x-demo", "a"])
    assert exc.value.returncode == 127
    assert r.ok(["no-such-program-x-demo", "a"]) is False
    out = capsys.readouterr().out
    assert f"+ {shlex.quote(sys.executable)} -c" in out


# --- setup base -----------------------------------------------------------------------------------------------------

def test_base_on_an_empty_project_enables_creates_and_binds_in_order(cfg, capsys):
    r = DryRunner()
    provision.base(cfg, r)
    apis = [f"{a}.googleapis.com" for a in ("run", "sqladmin", "artifactregistry", "cloudbuild", "secretmanager",
                                            "iam", "logging", "billingbudgets", "aiplatform")]
    expected = [
        g("services", "enable", *apis),
        g("artifacts", "repositories", "describe", "demo-repo", "--location", REGION),
        g("artifacts", "repositories", "create", "demo-repo", "--repository-format", "docker", "--location", REGION),
        g("iam", "service-accounts", "describe", RUN_SA),
        g("iam", "service-accounts", "create", "run-sa", "--display-name", "ShelfShift runtime"),
        g("iam", "service-accounts", "describe", DEPLOY_SA),
        g("iam", "service-accounts", "create", "deploy-sa", "--display-name", "ShelfShift deploy"),
    ] + [g("projects", "add-iam-policy-binding", PROJECT, "--member", f"serviceAccount:{sa}", "--role", role,
           "--condition", "None")
         for sa in (RUN_SA, DEPLOY_SA) for role in ("roles/cloudsql.client", "roles/cloudsql.instanceUser")]
    assert r.calls == expected
    assert capsys.readouterr().out.count("\n+ gcloud") + 1 >= len(expected)


def test_base_creates_nothing_that_exists_and_still_binds(cfg):
    r = DryRunner(rules=[(lambda a: "describe" in a, True)])
    provision.base(cfg, r)
    assert not [c for c in r.calls if "create" in c]
    assert sum("add-iam-policy-binding" in c for c in r.calls) == 4
    assert sum("enable" in c for c in r.calls) == 1


def test_base_carries_the_project_everywhere_and_grants_nothing_more(cfg):
    r = DryRunner()
    provision.base(cfg, r)
    for c in r.calls:
        assert c[0] == "gcloud" and c[-2:] == ["--project", PROJECT], c
        assert BILLING not in " ".join(c)
    roles = [c[c.index("--role") + 1] for c in r.calls if "--role" in c]
    assert sorted(set(roles)) == ["roles/cloudsql.client", "roles/cloudsql.instanceUser"]
    text = " ".join(" ".join(c) for c in r.calls)
    for absent in ("aiplatform.user", "bigquery", "storage", "buckets", "secretmanager.secretAccessor"):
        assert absent not in text


def _cli(*args: str, env_file: pathlib.Path | None = None):
    env = dict(os.environ, PATH=NO_GCLOUD_PATH)
    argv = [sys.executable, "-m", "deploy.provision", *args] + (["--env-file", str(env_file)] if env_file else [])
    return subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)


def test_the_cli_dry_run_needs_no_gcloud_and_exits_0(tmp_path):
    assert subprocess.run(["sh", "-c", "command -v gcloud"], env={"PATH": NO_GCLOUD_PATH},
                          capture_output=True).returncode != 0
    p = _cli("base", "--dry-run", env_file=synthetic_env.write(tmp_path / "env"))
    assert p.returncode == 0, p.stderr
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("+ ")]
    assert len(lines) == 11 and all(ln.startswith("+ gcloud ") for ln in lines)
    assert "bench-early.sh" in p.stdout.splitlines()[-1]
    assert BILLING not in p.stdout + p.stderr


def test_the_cli_refuses_an_invalid_configuration_before_any_command(tmp_path):
    bad = synthetic_env.write(tmp_path / "env", MIN_INSTANCES="2")
    p = _cli("base", "--dry-run", env_file=bad)
    assert p.returncode == 2
    assert p.stdout == ""
    assert p.stderr.startswith("config error: MIN_INSTANCES (line ")


def test_setup_sh_is_executable_bash_3_2_safe_and_hands_off_to_provision():
    sh = ROOT / "deploy" / "setup.sh"
    text = sh.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text and "deploy.provision" in text
    assert os.access(sh, os.X_OK)
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    for banned in ("mapfile", "readarray", "declare -A", "[@]", "=("):
        assert banned not in code
    assert subprocess.run(["/bin/bash", "-n", str(sh)]).returncode == 0


# --- setup data -----------------------------------------------------------------------------------------------------

AMOUNT = SYNTHETIC["BUDGET_AMOUNT_USD"]  # synthetic (12): no real budget amount is written in a test (06-05)
MARKER = "synthetic-passcode-1"
BUDGET_ID = "abc123"


def marker():
    return MARKER


def never():
    raise AssertionError("the passcode generator was called")


def is_(*lead: str):
    return lambda a: a[1:1 + len(lead)] == list(lead)


def budget_row(name="demo-budget", units=AMOUNT, nanos=None, budget_id=BUDGET_ID):
    amount = {"currencyCode": "USD", "units": units} | ({"nanos": nanos} if nanos is not None else {})
    return {"name": f"billingAccounts/{BILLING}/budgets/{budget_id}", "displayName": name,
            "amount": {"specifiedAmount": amount}}


FOUND = [(lambda a: "describe" in a and a[1:3] not in (["projects", "describe"], ["billing", "accounts"],
                                                      ["billing", "projects"]), True),
         (is_("sql", "users", "list"), [{"name": "run-sa@x-demo-12.iam"}, {"name": "deploy-sa@x-demo-12.iam"}])]


def project_number():
    return g("projects", "describe", PROJECT, "--format=value(projectNumber)")


def budget_create(number="<PROJECT_NUMBER>"):
    return g("billing", "budgets", "create", "--billing-account", BILLING, "--display-name", "demo-budget",
             "--budget-amount", f"{AMOUNT}USD", "--filter-projects", f"projects/{number}", "--calendar-period",
             "month", "--threshold-rule", "percent=0.5", "--threshold-rule", "percent=0.9", "--threshold-rule",
             "percent=1.0", "--billing-project", PROJECT)


def billing_link():
    return g("billing", "projects", "describe", PROJECT, "--format=value(billingAccountName)")


def billing_currency():
    return g("billing", "accounts", "describe", BILLING, "--format=value(currencyCode)")


def budget_list():
    return g("billing", "budgets", "list", "--billing-account", BILLING, "--billing-project", PROJECT) + \
        ["--format=json"]


# the release-data bucket (quick 261017-v60): described, created when missing, then its one reader bound every run
BUCKET_DESCRIBE = g("storage", "buckets", "describe", "gs://demo-data")
BUCKET_CREATE = g("storage", "buckets", "create", "gs://demo-data", "--location", REGION,
                  "--uniform-bucket-level-access", "--public-access-prevention")
BUCKET_BINDING = g("storage", "buckets", "add-iam-policy-binding", "gs://demo-data", "--member",
                   f"serviceAccount:{DEPLOY_SA}", "--role", "roles/storage.objectViewer")
BUCKET_STEPS = [BUCKET_DESCRIBE, BUCKET_CREATE, BUCKET_BINDING]


def test_data_on_an_empty_project_issues_every_step_in_order(cfg, capsys):
    r = DryRunner()
    assert provision.data(cfg, r, marker) == "created"
    expected = [
        g("sql", "instances", "describe", "demo-sql"),
        g("sql", "instances", "create", "demo-sql", "--database-version", "POSTGRES_17", "--edition", "ENTERPRISE",
          "--tier", "db-f1-micro", "--region", REGION, "--storage-size", "10", "--no-backup", "--availability-type",
          "zonal", "--database-flags", "cloudsql.iam_authentication=on"),
        g("sql", "databases", "describe", "demo_db", "--instance", "demo-sql"),
        g("sql", "databases", "create", "demo_db", "--instance", "demo-sql"),
        g("sql", "users", "list", "--instance", "demo-sql") + ["--format=json"],
        g("sql", "users", "create", "run-sa@x-demo-12.iam", "--instance", "demo-sql", "--type",
          "cloud_iam_service_account"),
        g("sql", "users", "create", "deploy-sa@x-demo-12.iam", "--instance", "demo-sql", "--type",
          "cloud_iam_service_account"),
        g("secrets", "describe", "demo-pass"),
        g("secrets", "create", "demo-pass", "--replication-policy", "automatic", "--data-file", "-"),
        g("secrets", "add-iam-policy-binding", "demo-pass", "--member", f"serviceAccount:{RUN_SA}", "--role",
          "roles/secretmanager.secretAccessor", "--condition", "None"),
        g("secrets", "describe", "demo-hq-pass"),  # the HQ secret is the user's (runbook step 6): never created here
        *BUCKET_STEPS,
        billing_link(),
        billing_currency(),
        budget_list(),
        project_number(),
        budget_create(),
    ]
    assert r.calls == expected
    assert [i for i, x in enumerate(r.inputs) if x] == [8]  # only the secret create takes stdin, withheld
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == "setup data: budget=created"
    assert lines[-2] == HQ_MISSING


HQ_MISSING = ("HQ_PASSCODE_SECRET names no secret yet: create it (deploy/README.md step 6, The HQ secret), then rerun "
              "bash deploy/setup.sh data. Nothing was created.")
HQ_BINDING = g("secrets", "add-iam-policy-binding", "demo-hq-pass", "--member", f"serviceAccount:{RUN_SA}", "--role",
               "roles/secretmanager.secretAccessor", "--condition", "None")


def test_an_existing_hq_secret_gets_the_run_account_read_access_and_nothing_else(cfg, capsys):
    r = DryRunner(rules=FOUND + [(is_("billing", "budgets", "list"), [budget_row()])])
    provision.data(cfg, r, never)
    hq = [c for c in r.calls if "demo-hq-pass" in c]
    assert hq == [g("secrets", "describe", "demo-hq-pass"), HQ_BINDING]
    store_binding = r.calls.index(next(c for c in r.calls if c[1:3] == ["secrets", "add-iam-policy-binding"]))
    assert r.calls[store_binding + 1:store_binding + 3] == hq  # right after the passcode secret's binding
    assert HQ_MISSING not in capsys.readouterr().out


def test_a_missing_hq_secret_is_reported_with_the_runbook_step_and_never_created(cfg, capsys):
    hq_absent = [(lambda a: a[1:4] == ["secrets", "describe", "demo-hq-pass"], False)]
    r = DryRunner(rules=hq_absent + FOUND + [(is_("billing", "budgets", "list"), [budget_row()])])
    assert provision.data(cfg, r, never) == "unchanged"
    assert [c for c in r.calls if "demo-hq-pass" in c] == [g("secrets", "describe", "demo-hq-pass")]
    assert r.calls[-1][1:3] == ["billing", "budgets"]  # the run went on to the budget
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == "setup data: budget=unchanged" and lines[-2] == HQ_MISSING


def test_new_passcode_never_touches_the_hq_secret(cfg):
    r = DryRunner(rules=FOUND)
    provision.data(cfg, r, marker, rotate=True)
    assert [c for c in r.calls if "demo-hq-pass" in c] == [g("secrets", "describe", "demo-hq-pass"), HQ_BINDING]
    assert [c[4] for c in r.calls if c[1:4] == ["secrets", "versions", "add"]] == ["demo-pass"]


def test_the_cli_data_dry_run_prints_the_hq_describe_and_binding_and_says_it_never_creates_it(tmp_path):
    p = _cli("data", "--dry-run", env_file=synthetic_env.write(tmp_path / "env"))
    assert p.returncode == 0, p.stderr
    assert "+ gcloud secrets describe demo-hq-pass --project x-demo-12" in p.stdout
    assert "+ gcloud secrets add-iam-policy-binding demo-hq-pass --member" in p.stdout
    assert not re.search(r"secrets (create|versions add) demo-hq-pass", p.stdout)
    assert "a real run never creates the HQ secret" in p.stdout
    assert p.stdout.splitlines()[-1].startswith("setup data: budget=")


def test_the_budget_filter_names_the_project_by_number(cfg, capsys):
    """The Budget API writes its project filter as projects/<number>."""
    r = DryRunner(rules=[(is_("projects", "describe"), "123456789\n")])
    assert provision.data(cfg, r, marker) == "created"
    assert [c for c in r.calls if c[1:4] == ["billing", "budgets", "create"]] == [budget_create("123456789")]


def test_a_project_billed_to_another_account_goes_to_the_console_and_leaves_budgets_alone(cfg, capsys):
    """A budget filters only projects on its own account (INVALID_ARGUMENT, 2026-10-16); the other ID is never printed."""
    other = "000000-111111-222222"
    r = DryRunner(rules=[(is_("billing", "projects", "describe"), f"billingAccounts/{other}\n")])
    assert provision.data(cfg, r, marker) == "manual"
    assert not [c for c in r.calls if c[1:3] in (["billing", "budgets"], ["billing", "accounts"])]
    out = capsys.readouterr().out
    assert "budget: not set by this script (PROJECT is billed to another billing account than BILLING_ACCOUNT)" in out
    assert other not in out and BILLING not in out and out.splitlines()[-1] == "setup data: budget=manual"


def test_the_project_own_account_goes_on_to_the_budget(cfg, capsys):
    r = DryRunner(rules=[(is_("billing", "projects", "describe"), f"billingAccounts/{BILLING}\n")])
    assert provision.data(cfg, r, marker) == "created"


def test_a_billing_account_in_another_currency_goes_to_the_console_and_leaves_budgets_alone(cfg, capsys):
    """A USD amount on a non-USD account is refused (INVALID_ARGUMENT, 2026-10-16); no conversion, no update."""
    r = DryRunner(rules=[(is_("billing", "accounts", "describe"), "EUR\n"),
                         (is_("billing", "budgets", "list"), [budget_row(units="1000")])])
    assert provision.data(cfg, r, marker) == "manual"
    assert not [c for c in r.calls if c[1:3] == ["billing", "budgets"]]
    out = capsys.readouterr().out
    assert "budget: not set by this script (the billing account bills in EUR, not USD)" in out
    assert "enter the EUR equivalent of BUDGET_AMOUNT_USD" in out
    assert BILLING not in out and out.splitlines()[-1] == "setup data: budget=manual"


def test_the_passcode_is_never_printed_or_recorded(cfg, capsys):
    r = DryRunner()
    provision.data(cfg, r, marker)
    out = capsys.readouterr()
    assert MARKER not in out.out + out.err
    assert MARKER not in repr(r.calls) and MARKER not in repr(r.inputs)
    assert "< (withheld)" in out.out


def test_only_the_budget_commands_carry_the_billing_account_and_none_is_printed(cfg, capsys):
    r = DryRunner()
    provision.data(cfg, r, marker)
    out = capsys.readouterr().out
    carrying = [c for c in r.calls if BILLING in c]
    assert carrying == [billing_currency(), budget_list(), budget_create()]
    for c in carrying[1:]:
        assert c[c.index("--billing-project") + 1] == PROJECT
    assert BILLING not in out  # printed commands show <BILLING_ACCOUNT> (D-04)
    assert "--billing-account '<BILLING_ACCOUNT>'" in out or "--billing-account <BILLING_ACCOUNT>" in out


def test_data_with_everything_present_creates_nothing_and_never_generates(cfg, capsys):
    r = DryRunner(rules=FOUND + [(is_("billing", "budgets", "list"), [budget_row()])])
    assert provision.data(cfg, r, never) == "unchanged"
    assert not [c for c in r.calls if "create" in c or "update" in c or "versions" in c]
    # every binding is repeated: the passcode secret's, the bucket's and the HQ secret's (the user's, never created)
    assert sum("add-iam-policy-binding" in c for c in r.calls) == 3
    assert BUCKET_BINDING in r.calls
    out = capsys.readouterr().out
    assert out.splitlines()[-1] == "setup data: budget=unchanged"
    assert "--new-passcode" in out  # how to rotate it is said, not done


def test_the_release_data_bucket_has_one_reader_and_no_project_storage_role(cfg):
    """T-v60-02, T-v60-03: the deploy account reads DATA_BUCKET, on that bucket only; no account writes to it, the run
    account is never named on it, and no storage role is granted on the project. setup base is unchanged."""
    r = DryRunner()
    provision.data(cfg, r, marker)
    storage = [c for c in r.calls if c[1] == "storage"]
    assert storage == BUCKET_STEPS
    granted = [c for c in r.calls if "--role" in c and "storage" in c[c.index("--role") + 1]]
    assert granted == [BUCKET_BINDING]
    assert not [c for c in storage if RUN_SA in " ".join(c)]
    assert not [c for c in r.calls if c[1:3] == ["projects", "add-iam-policy-binding"]]
    assert "allUsers" not in json.dumps(r.calls)
    b = DryRunner()
    provision.base(cfg, b)
    assert not [c for c in b.calls if c[1] == "storage" or "storage" in " ".join(c)]


def test_new_passcode_adds_a_version_only_when_the_secret_exists(cfg):
    calls = []

    def counted():
        calls.append(1)
        return MARKER

    r = DryRunner(rules=FOUND)
    provision.data(cfg, r, counted, rotate=True)
    added = [i for i, c in enumerate(r.calls) if c[1:4] == ["secrets", "versions", "add"]]
    assert [r.calls[i] for i in added] == [g("secrets", "versions", "add", "demo-pass", "--data-file", "-")]
    assert r.inputs[added[0]] == "(withheld)" and len(calls) == 1
    assert not [c for c in r.calls if c[1:3] == ["secrets", "create"]]

    calls.clear()
    r = DryRunner()  # absent: rotate makes no difference, one create
    provision.data(cfg, r, counted, rotate=True)
    assert sum(c[1:3] == ["secrets", "create"] for c in r.calls) == 1
    assert not [c for c in r.calls if "versions" in c] and len(calls) == 1


@pytest.mark.parametrize("rows, result, verb", [
    ([], "created", "create"),
    ([budget_row()], "unchanged", None),
    ([budget_row(units="7")], "updated", "update"),
    ([budget_row(units=AMOUNT, nanos=500000000)], "updated", "update"),
    ([budget_row(name="other")], "created", "create"),
])
def test_the_budget_is_created_updated_or_left_alone(cfg, capsys, rows, result, verb):
    r = DryRunner(rules=[(is_("billing", "budgets", "list"), rows)])
    assert provision.data(cfg, r, marker) == result
    changes = [c for c in r.calls if c[1:3] == ["billing", "budgets"] and c[3] in ("create", "update")]
    assert [c[3] for c in changes] == ([verb] if verb else [])
    if verb == "update":
        assert changes[0] == g("billing", "budgets", "update", BUDGET_ID, "--billing-account", BILLING,
                               "--budget-amount", f"{AMOUNT}USD", "--billing-project", PROJECT)
    assert capsys.readouterr().out.splitlines()[-1] == f"setup data: budget={result}"


def assert_continued_manually(out: str, reason: str) -> None:
    """06-REVIEWS R1-2: the console steps, then every closing line, and the resume line last."""
    assert f"budget: not set by this script ({reason}); create it in the console:" in out
    assert "Budgets & alerts" in out and "BUDGET_NAME" in out and "BUDGET_AMOUNT_USD" in out
    assert "50%, 90% and 100%" in out
    assert '\nGRANT USAGE, CREATE ON SCHEMA public TO "deploy-sa@x-demo-12.iam";\n' in out
    assert 'gcloud secrets versions access latest --secret "$PASSCODE_SECRET" --project "$PROJECT"' in out
    assert "bash deploy/release.sh" in out
    assert BILLING not in out
    assert out.splitlines()[-1] == "setup data: budget=manual"


@pytest.mark.parametrize("failing, reason", [
    ("list", "gcloud billing budgets list exited 1"),
    ("create", "gcloud billing budgets create exited 1"),
])
def test_a_failing_budget_command_never_blocks_provisioning(tmp_path, capsys, failing, reason):
    env = synthetic_env.write(tmp_path / "env")
    r = DryRunner(fail=is_("billing", "budgets", failing))
    assert provision.main(["data", "--dry-run", "--env-file", str(env)], runner=r) == 0
    out = capsys.readouterr().out
    assert_continued_manually(out, reason)
    assert not [c for c in r.calls if c[1:4] == ["billing", "budgets", "update"]]


def test_two_budgets_with_the_name_are_never_guessed_at(tmp_path, capsys):
    env = synthetic_env.write(tmp_path / "env")
    r = DryRunner(rules=[(is_("billing", "budgets", "list"), [budget_row(budget_id="a"), budget_row(budget_id="b")])])
    assert provision.main(["data", "--dry-run", "--env-file", str(env)], runner=r) == 0
    out = capsys.readouterr().out
    assert_continued_manually(out, "two budgets named BUDGET_NAME")
    assert not [c for c in r.calls if c[1:3] == ["billing", "budgets"] and c[3] in ("create", "update")]


def test_the_closing_lines_give_the_grants_the_read_command_and_the_resume_line_last(cfg, capsys):
    provision.data(cfg, DryRunner(), marker)
    lines = capsys.readouterr().out.splitlines()
    i = lines.index('GRANT USAGE, CREATE ON SCHEMA public TO "deploy-sa@x-demo-12.iam";')
    assert lines[i + 1] == 'GRANT USAGE ON SCHEMA public TO "run-sa@x-demo-12.iam";'
    before = "\n".join(lines[:i])
    assert ("gcloud sql users set-password postgres --instance demo-sql --prompt-for-password --project x-demo-12"
            in before)
    assert "gcloud sql connect demo-sql --user postgres --database demo_db --project x-demo-12" in before
    assert "before the first build job" in before
    after = "\n".join(lines[i:])
    assert 'gcloud secrets versions access latest --secret "$PASSCODE_SECRET" --project "$PROJECT"' in after
    assert "bash deploy/release.sh" in after and "never stops spending" in after
    assert lines[-1] == "setup data: budget=created"


def test_grant_sql_is_two_schema_grants_with_validated_names(cfg):
    assert provision.grant_sql(cfg) == ['GRANT USAGE, CREATE ON SCHEMA public TO "deploy-sa@x-demo-12.iam";',
                                        'GRANT USAGE ON SCHEMA public TO "run-sa@x-demo-12.iam";']
    with pytest.raises(ValueError):
        provision.grant_sql(dict(cfg, RUN_SA_EMAIL='a"b@x-demo-12.iam.gserviceaccount.com'))


def test_the_role_pattern_is_migrates():
    from api import db
    assert provision.ROLE_NAME.pattern == db.ROLE_NAME.pattern


def test_new_passcode_is_10_characters_from_the_alphabet_and_never_repeats():
    assert provision.PASSCODE_LENGTH == 10 >= 8
    alphabet = provision.PASSCODE_ALPHABET
    assert len(alphabet) == len(set(alphabet)) == 31
    assert not set(alphabet) & set("0o1li") and set(alphabet) <= set("abcdefghijklmnopqrstuvwxyz0123456789")
    codes = [provision.new_passcode() for _ in range(1000)]
    assert len(set(codes)) == 1000
    assert all(len(c) == 10 and set(c) <= set(alphabet) for c in codes)


def test_new_passcode_draws_from_secrets_only(monkeypatch):
    import random
    import secrets

    def broken(*a, **k):
        raise AssertionError("random was used")

    for name in ("choice", "choices", "sample", "random", "randint", "randrange", "getrandbits", "shuffle"):
        monkeypatch.setattr(random, name, broken)
    assert len(provision.new_passcode()) == 10
    drawn = []
    real = secrets.choice
    monkeypatch.setattr(secrets, "choice", lambda seq: drawn.append(seq) or real(seq))
    provision.new_passcode()
    assert drawn == [provision.PASSCODE_ALPHABET] * 10
    assert "random" not in vars(provision)


def test_the_dry_run_cli_never_generates_a_passcode(tmp_path, capsys, monkeypatch):
    import secrets
    monkeypatch.setattr(secrets, "choice", never)
    monkeypatch.setattr(provision, "new_passcode", never)
    env = synthetic_env.write(tmp_path / "env")
    assert provision.main(["data", "--dry-run", "--env-file", str(env)]) == 0
    out = capsys.readouterr().out
    assert provision.DRY_RUN_STDIN not in out
    assert out.splitlines()[-1] == "setup data: budget=created"


def test_the_data_cli_dry_run_needs_no_gcloud_and_no_input(tmp_path):
    env = synthetic_env.write(tmp_path / "env")
    p = subprocess.run([sys.executable, "-m", "deploy.provision", "data", "--dry-run", "--env-file", str(env)],
                       cwd=ROOT, env=dict(os.environ, PATH=NO_GCLOUD_PATH), stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert p.stdout.splitlines()[-1] == "setup data: budget=created"
    assert BILLING not in p.stdout + p.stderr
    printed = [shlex.split(line[2:]) for line in p.stdout.splitlines() if line.startswith("+ gcloud storage ")]
    assert printed == BUCKET_STEPS[:1] + [BUCKET_CREATE] + [BUCKET_BINDING]
    p = subprocess.run([sys.executable, "-m", "deploy.provision", "base", "--new-passcode", "--dry-run",
                        "--env-file", str(env)], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert p.returncode == 2  # --new-passcode belongs to data only


def test_a_failed_command_outside_the_budget_stops_data_with_exit_1(tmp_path, capsys):
    env = synthetic_env.write(tmp_path / "env")
    r = DryRunner(fail=is_("sql", "instances", "create"))
    assert provision.main(["data", "--dry-run", "--env-file", str(env)], runner=r) == 1
    out = capsys.readouterr()
    assert "gcloud sql instances create" in out.err
    assert "setup data: budget=" not in out.out
    assert not [c for c in r.calls if c[1] == "secrets"]
