"""Provisioning written out as commands the user runs (OPS-08, OPS-07, OPS-02; 06-CONTEXT D-02, D-04, D-05, D-15,
D-24; 06-REVIEWS R1-2).

The user runs `bash deploy/setup.sh <mode>` in their own terminal; Claude never does. Claude only runs
`--dry-run --env-file <synthetic file>`, which prints every command, runs nothing and needs no gcloud. Every value
comes from the validated configuration (deploy.envfile.load), and every gcloud command names its project.

setup base (before the database exists, so the early bench can run first; D-15):
    1. gcloud services enable: run, sqladmin, artifactregistry, cloudbuild, secretmanager, iam, logging,
       billingbudgets, aiplatform
    2. the Artifact Registry repository AR_REPO (docker, REGION), when missing
    3. the run and deploy service accounts (RUN_SA_EMAIL, DEPLOY_SA_EMAIL), when missing
    4. each account gets roles/cloudsql.client and roles/cloudsql.instanceUser on the project, nothing more

setup data [--new-passcode] (after the early bench):
    1. the Cloud SQL instance SQL_INSTANCE, when missing: PostgreSQL 17, ENTERPRISE, db-f1-micro, 10 GB, zonal, no
       backups (every build rebuilds from the seed), cloudsql.iam_authentication=on
    2. the database DB_NAME, and both accounts' IAM database users (the email without .gserviceaccount.com)
    3. the passcode secret PASSCODE_SECRET, when missing, holding a passcode this script generates (D-24): 10
       characters drawn with `secrets` from an alphabet without look-alikes, handed to gcloud on stdin
       (--data-file -), never in argv, never printed, logged or written anywhere. An existing secret is left alone
       unless --new-passcode adds a new version the same way. The user reads it with their own command (printed).
    4. the run account's roles/secretmanager.secretAccessor on that one secret and on the HQ secret
       (HQ_PASSCODE_SECRET), which the user creates (runbook step 6, The HQ secret; 06.1 D-47, HQ handoff item 6):
       it is described, and bound only when it exists; it is never created, versioned or read here, and
       --new-passcode touches PASSCODE_SECRET only. A missing one is reported in the closing lines with the runbook
       step, and the run goes on. A dry run prints the describe and the binding as a real run on a project where it
       exists, and says a real run never creates it
   4a. the release-data bucket gs://DATA_BUCKET (quick 261017-v60), when missing: in REGION, uniform bucket-level
       access, public access prevention enforced; then the deploy account's roles/storage.objectViewer on that one
       bucket (repeated every run), and nothing more. The build job reads the public set from it through a read-only
       volume; the release uploads with the user's own login, so no account is granted any write role
    5. the budget (`budget`): BUDGET_NAME for BUDGET_AMOUNT_USD a month on this project, alerts at 50%, 90% and 100%;
       created when missing, its amount updated when it differs, else left alone. It never blocks provisioning:
       a failing budget command (most often a missing billing role) or two budgets with that name prints the
       console steps instead and the result is `manual` (R1-2). BILLING_ACCOUNT is used by these commands only, and
       is printed as <BILLING_ACCOUNT> (D-04).
    6. closing lines: the two schema grants (`grant_sql`) and the two commands to apply them by hand once, before the
       first build job; the passcode read command; the next step; and last, the resume line
       `setup data: budget=<created|updated|unchanged|manual>`, right after the missing-HQ-secret line when there is
       one.

The grant split (06-RESEARCH Pitfall 5): the schema grants here are applied once by hand, as postgres; the build job's
migrate() grants the runtime user every table and sequence at every build (api/db.py), so a clone never silently
skips a table information_schema would hide.

Describe-or-create: a resource that exists is left as it is, so either mode is safe to re-run. A role binding is
repeated every run (adding a binding that exists changes nothing); if one fails right after its account was created,
the account has not reached IAM yet: wait a minute and re-run.

Deliberately absent (least privilege, D-04): the Phase 7 uploads bucket and its role arrive in Phase 7, and Gemini's
roles/aiplatform.user in Phase 9. Enabling the aiplatform API grants nobody anything.

CLI: python -m deploy.provision {base,data} [--dry-run] [--env-file PATH]; data also takes --new-passcode.
A refused configuration prints "config error: ..." lines on stderr and exits 2 before any command; a failed command
(other than the budget's) stops the run with exit 1 (fix the cause and re-run).
"""
from __future__ import annotations

import argparse
import decimal
import pathlib
import re
import secrets
import sys
from typing import Callable

from deploy import envfile
from deploy.cloud import CommandError, DryRunner, Runner, gcloud

APIS = ("run", "sqladmin", "artifactregistry", "cloudbuild", "secretmanager", "iam", "logging", "billingbudgets",
        "aiplatform")
PROJECT_ROLES = ("roles/cloudsql.client", "roles/cloudsql.instanceUser")  # each account's only project roles
ACCOUNTS = (("RUN_SA_EMAIL", "ShelfShift runtime"), ("DEPLOY_SA_EMAIL", "ShelfShift deploy"))

# D-24: the passcode is generated, never typed: at least 8 random characters (10 here, about 49 bits from 31 symbols),
# without look-alike characters (0, o, 1, l, i) because judges type it from a slide.
PASSCODE_LENGTH = 10
PASSCODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
DRY_RUN_STDIN = "(dry run: no passcode generated)"  # what the dry run hands its DryRunner, which never prints it
ROLE_NAME = re.compile(r"[a-z0-9][a-z0-9._@-]{0,62}")  # the same pattern as api.db.ROLE_NAME (a test checks)
BILLING_LABEL = "<BILLING_ACCOUNT>"


def new_passcode() -> str:
    """A fresh passcode from the standard library's CSPRNG (secrets), PASSCODE_LENGTH characters."""
    return "".join(secrets.choice(PASSCODE_ALPHABET) for _ in range(PASSCODE_LENGTH))


def base(cfg: dict[str, str], runner: Runner) -> None:
    """APIs, the image repository, the two service accounts and their two project roles each."""
    project = cfg["PROJECT"]
    runner.run(gcloud("services", "enable", *(f"{api}.googleapis.com" for api in APIS), project=project))
    repo = cfg["AR_REPO"]
    if not runner.ok(gcloud("artifacts", "repositories", "describe", repo, "--location", cfg["REGION"],
                            project=project)):
        runner.run(gcloud("artifacts", "repositories", "create", repo, "--repository-format", "docker",
                          "--location", cfg["REGION"], project=project))
    for key, display in ACCOUNTS:
        email = cfg[key]
        if not runner.ok(gcloud("iam", "service-accounts", "describe", email, project=project)):
            runner.run(gcloud("iam", "service-accounts", "create", email.split("@", 1)[0], "--display-name", display,
                              project=project))
    for key, _ in ACCOUNTS:
        for role in PROJECT_ROLES:
            runner.run(gcloud("projects", "add-iam-policy-binding", project, "--member",
                              f"serviceAccount:{cfg[key]}", "--role", role, "--condition", "None", project=project))


def grant_sql(cfg: dict[str, str]) -> list[str]:
    """The two schema grants the user applies once, as postgres, before the first build job."""
    deploy, run = (envfile.iam_db_user(cfg[k]) for k in ("DEPLOY_SA_EMAIL", "RUN_SA_EMAIL"))
    for role in (deploy, run):
        if not ROLE_NAME.fullmatch(role):
            raise ValueError("a service account's IAM database user is not a valid role name")
    return [f'GRANT USAGE, CREATE ON SCHEMA public TO "{deploy}";', f'GRANT USAGE ON SCHEMA public TO "{run}";']


def _amount(row: dict) -> decimal.Decimal | None:
    spec = (row.get("amount") or {}).get("specifiedAmount")
    if not spec:
        return None
    return decimal.Decimal(str(spec.get("units") or 0)) + decimal.Decimal(spec.get("nanos") or 0) / 10 ** 9


def _console_steps(reason: str) -> None:
    print(f"budget: not set by this script ({reason}); create it in the console:")
    print("  1. Billing > Budgets & alerts > Create budget")
    print("  2. Name: the value of BUDGET_NAME")
    print("  3. Scope: this project only (the value of PROJECT)")
    print("  4. Amount: the value of BUDGET_AMOUNT_USD, in US dollars, every month")
    print("  5. Alert thresholds: 50%, 90% and 100% of actual spend")
    if reason.startswith("two budgets"):
        print("  (Two exist already: keep one, delete the other, and check the one kept matches these steps.)")


def budget(cfg: dict[str, str], runner: Runner) -> str:
    """Create, update or leave the budget; `created`, `updated`, `unchanged`, or `manual` when it cannot (R1-2)."""
    project, account, name = cfg["PROJECT"], cfg["BILLING_ACCOUNT"], cfg["BUDGET_NAME"]
    runner.hide(account, BILLING_LABEL)
    amount = f"{cfg['BUDGET_AMOUNT_USD']}USD"
    quota = ("--billing-project", project)
    try:
        # A budget can filter only projects billed to its own account (INVALID_ARGUMENT otherwise, seen 2026-10-16
        # when BILLING_ACCOUNT was not the project's account). A dry run answers nothing and goes on.
        linked = runner.run(gcloud("billing", "projects", "describe", project, "--format=value(billingAccountName)",
                                   project=project)).strip().removeprefix("billingAccounts/")
        if linked and linked != account:
            _console_steps("PROJECT is billed to another billing account than BILLING_ACCOUNT")
            print("  Or set BILLING_ACCOUNT to the project's own account (gcloud billing projects describe) and rerun.")
            return "manual"
        # A budget's amount must be in the billing account's currency (INVALID_ARGUMENT otherwise, seen 2026-10-16
        # on a non-USD account). No conversion here: a non-USD account goes to the console, and an existing budget is
        # never updated in the wrong currency. A dry run answers nothing and goes on as USD.
        currency = runner.run(gcloud("billing", "accounts", "describe", account, "--format=value(currencyCode)",
                                     project=project)).strip()
        if currency and currency != "USD":
            _console_steps(f"the billing account bills in {currency}, not USD")
            print(f"  The console asks for {currency}: enter the {currency} equivalent of BUDGET_AMOUNT_USD.")
            return "manual"
        found = runner.json(gcloud("billing", "budgets", "list", "--billing-account", account, *quota,
                                   project=project)) or []
        same = [b for b in found if b.get("displayName") == name]
        if len(same) > 1:
            _console_steps("two budgets named BUDGET_NAME")
            return "manual"
        if not same:
            # The Budget API writes its project filter as projects/<number> (its docs: projects/123456789). A dry run
            # answers nothing, so it shows the placeholder.
            number = runner.run(gcloud("projects", "describe", project, "--format=value(projectNumber)",
                                       project=project)).strip() or "<PROJECT_NUMBER>"
            runner.run(gcloud("billing", "budgets", "create", "--billing-account", account, "--display-name", name,
                              "--budget-amount", amount, "--filter-projects", f"projects/{number}",
                              "--calendar-period", "month", "--threshold-rule", "percent=0.5", "--threshold-rule",
                              "percent=0.9", "--threshold-rule", "percent=1.0", *quota, project=project))
            return "created"
        if _amount(same[0]) == decimal.Decimal(cfg["BUDGET_AMOUNT_USD"]):
            return "unchanged"
        budget_id = same[0]["name"].rsplit("/", 1)[-1]
        runner.run(gcloud("billing", "budgets", "update", budget_id, "--billing-account", account,
                          "--budget-amount", amount, *quota, project=project))
        return "updated"
    except CommandError as exc:
        _console_steps(f"{exc.argv0} exited {exc.returncode}")
        if exc.stderr_tail:
            print("  gcloud said: " + runner.mask(exc.stderr_tail).replace("\n", "\n  "))
        return "manual"


HQ_MISSING = ("HQ_PASSCODE_SECRET names no secret yet: create it (deploy/README.md step 6, The HQ secret), then rerun "
              "bash deploy/setup.sh data. Nothing was created.")
HQ_DRY_RUN = ("HQ secret: the dry run shows its read access as on a project where it exists; a real run never "
              "creates the HQ secret (deploy/README.md step 6, The HQ secret).")


def hq_secret_access(cfg: dict[str, str], runner: Runner, *, dry_run: bool = False) -> bool:
    """The run account's read access on the HQ secret, which the user creates (runbook step 6): describe it, and bind
    roles/secretmanager.secretAccessor only when it exists. Never creates, versions or reads it. Whether it exists
    (a dry run answers as a project where it does)."""
    secret = cfg["HQ_PASSCODE_SECRET"]
    exists = runner.ok(gcloud("secrets", "describe", secret, project=cfg["PROJECT"])) or dry_run
    if exists:
        runner.run(gcloud("secrets", "add-iam-policy-binding", secret, "--member",
                          f"serviceAccount:{cfg['RUN_SA_EMAIL']}", "--role", "roles/secretmanager.secretAccessor",
                          "--condition", "None", project=cfg["PROJECT"]))
    return exists


def data(cfg: dict[str, str], runner: Runner, new_passcode: Callable[[], str], *, rotate: bool = False,
         dry_run: bool = False) -> str:
    """Cloud SQL with IAM login, the database and its IAM users, the passcode secret, read access on the HQ secret,
    the release-data bucket and its reader, the budget, then the closing lines with the resume line last. Returns the
    budget result."""
    project, instance, db_name, secret = cfg["PROJECT"], cfg["SQL_INSTANCE"], cfg["DB_NAME"], cfg["PASSCODE_SECRET"]
    runner.hide(cfg["BILLING_ACCOUNT"], BILLING_LABEL)
    if not runner.ok(gcloud("sql", "instances", "describe", instance, project=project)):
        runner.run(gcloud("sql", "instances", "create", instance, "--database-version", "POSTGRES_17", "--edition",
                          "ENTERPRISE", "--tier", "db-f1-micro", "--region", cfg["REGION"], "--storage-size", "10",
                          "--no-backup", "--availability-type", "zonal", "--database-flags",
                          "cloudsql.iam_authentication=on", project=project))
    if not runner.ok(gcloud("sql", "databases", "describe", db_name, "--instance", instance, project=project)):
        runner.run(gcloud("sql", "databases", "create", db_name, "--instance", instance, project=project))
    users = {u.get("name") for u in runner.json(gcloud("sql", "users", "list", "--instance", instance,
                                                       project=project)) or []}
    for key, _ in ACCOUNTS:
        user = envfile.iam_db_user(cfg[key])
        if user not in users:
            runner.run(gcloud("sql", "users", "create", user, "--instance", instance, "--type",
                              "cloud_iam_service_account", project=project))
    made = "would be generated (dry run)" if isinstance(runner, DryRunner) else "was generated"
    if not runner.ok(gcloud("secrets", "describe", secret, project=project)):
        runner.run(gcloud("secrets", "create", secret, "--replication-policy", "automatic", "--data-file", "-",
                          project=project), stdin=new_passcode())
        passcode_note = f"a new passcode {made} and stored as the secret's first version; nothing prints it."
    elif rotate:
        runner.run(gcloud("secrets", "versions", "add", secret, "--data-file", "-", project=project),
                   stdin=new_passcode())
        passcode_note = f"a new passcode {made} and added as the secret's latest version; nothing prints it."
    else:
        passcode_note = "the secret exists and was left alone (pass --new-passcode to add a new one)."
    runner.run(gcloud("secrets", "add-iam-policy-binding", secret, "--member", f"serviceAccount:{cfg['RUN_SA_EMAIL']}",
                      "--role", "roles/secretmanager.secretAccessor", "--condition", "None", project=project))
    hq_found = hq_secret_access(cfg, runner, dry_run=dry_run)
    bucket = f"gs://{cfg['DATA_BUCKET']}"  # the release-data bucket (4a): private, its one reader the deploy account
    if not runner.ok(gcloud("storage", "buckets", "describe", bucket, project=project)):
        runner.run(gcloud("storage", "buckets", "create", bucket, "--location", cfg["REGION"],
                          "--uniform-bucket-level-access", "--public-access-prevention", project=project))
    runner.run(gcloud("storage", "buckets", "add-iam-policy-binding", bucket, "--member",
                      f"serviceAccount:{cfg['DEPLOY_SA_EMAIL']}", "--role", "roles/storage.objectViewer",
                      project=project))
    result = budget(cfg, runner)

    print()
    print("Schema grants: apply them once, before the first build job (the runtime user must be able to read what "
          "the build job creates). In your own terminal:")
    print(f"  gcloud sql users set-password postgres --instance {instance} --prompt-for-password --project {project}")
    print(f"  gcloud sql connect {instance} --user postgres --database {db_name} --project {project}")
    print("then paste these two lines at the postgres prompt:")
    for stmt in grant_sql(cfg):
        print(stmt)
    print("(Every build job grants the runtime user its table and sequence privileges itself.)")
    print()
    print(f"Passcode: {passcode_note}")
    print("To read it, run this in a terminal outside Claude Code, so it stays out of any transcript:")
    print('  eval "$(.venv/bin/python -m deploy.envfile export)" && '
          'gcloud secrets versions access latest --secret "$PASSCODE_SECRET" --project "$PROJECT"')
    print()
    print("Next (runbook): bash deploy/release.sh")
    print("The budget alert only notifies; it never stops spending (D-02).")
    if dry_run:
        print(HQ_DRY_RUN)
    elif not hq_found:
        print(HQ_MISSING)
    print(f"setup data: budget={result}")
    return result


NEXT = {
    "base": "Next (runbook): the model metadata check, then bash deploy/bench-early.sh",
}


def main(argv: list[str] | None = None, *, runner: Runner | None = None) -> int:
    """`runner` lets a test hand in a scripted DryRunner; the CLI makes its own."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true",
                        help="print every command and run nothing (needs no gcloud)")
    common.add_argument("--env-file", type=pathlib.Path, default=None,
                        help="the configuration to read (default deploy/.env.local)")
    ap = argparse.ArgumentParser(prog="bash deploy/setup.sh", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("base", parents=[common], help="APIs, image repository, service accounts, their roles")
    data_p = sub.add_parser("data", parents=[common],
                            help="Cloud SQL, database, IAM users, passcode secret, budget, grant SQL")
    data_p.add_argument("--new-passcode", action="store_true",
                        help="add a newly generated passcode as a new version of an existing secret")
    args = ap.parse_args(argv)
    try:
        cfg = envfile.load(args.env_file or envfile.LOCAL)  # LOCAL read at call time
    except envfile.ConfigError as exc:
        for problem in str(exc).splitlines():
            print(f"config error: {problem}", file=sys.stderr)
        return 2
    if runner is None:
        runner = DryRunner() if args.dry_run else Runner()
    if args.dry_run:
        print(f"dry run: the commands setup {args.mode} would run; nothing is run")
    try:
        if args.mode == "base":
            base(cfg, runner)
        else:
            # The dry run never generates a passcode: its DryRunner gets a marker it never prints.
            generator = (lambda: DRY_RUN_STDIN) if args.dry_run else new_passcode
            data(cfg, runner, generator, rotate=args.new_passcode, dry_run=args.dry_run)
            return 0  # data's last line is the resume line; nothing is printed after it
    except CommandError as exc:
        print(f"setup {args.mode} stopped: {exc}", file=sys.stderr)
        print("Fix the cause and re-run; finished steps are skipped.", file=sys.stderr)
        return 1
    done = "dry run complete, nothing was run" if args.dry_run else "done"
    print(f"setup {args.mode}: {done}. {NEXT[args.mode]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
