"""The one deploy configuration: load it, refuse a bad one before any cloud command, export it to a shell
(06-CONTEXT D-03, D-04, D-07, D-08).

deploy/.env.example (committed) names every key with a one-line explanation. Each key that identifies the project,
the billing account, a person, the budget or a resource holds a <placeholder> there; only the decided public values
(REGION, GEMINI_MODEL, VERTEX_LOCATION, MIN_INSTANCES, GLOBAL_AI_CAP) are written as values. The user copies it to
deploy/.env.local (git-ignored, never in an image or a Cloud Build upload, never read by Claude) and fills it.

`load` reads a filled copy whose keys must be exactly the example's and refuses, all problems at once: a line that is
not a comment, a blank line or a plain KEY=value, a quoted value, a duplicate key, an unknown key, a missing key, an
empty value (only CONTACT_EMAIL, until Phase 11, and RELEASE_DATA_DIR, for a release without the public set, may stay
empty), a value still holding a <placeholder>, a value that fails its key's rule in RULES, and an HQ_PASSCODE_SECRET
that names the same secret as PASSCODE_SECRET (one secret for both credentials would make the HQ passcode the store
passcode; 06-REVIEW IN-05). Every message names the key and its line, never the value (D-07). Each rule is strict
enough that no accepted value holds a quote, a space or a shell metacharacter (an email's plus aside, and
RELEASE_DATA_DIR's single spaces between path words, as a local folder may hold one), and the export quotes every
value with shlex.quote anyway.

Python rather than shell parsing, because macOS ships bash 3.2 (06-RESEARCH Pitfall 12) and a loader can be tested.
Max instances is not a key: the release passes the constant 1 (OPS-02). BILLING_ACCOUNT is for the budget only and
never reaches an image or a service (D-04).

CLI (the deploy scripts eval the export; docs name these commands, never a value):
    python -m deploy.envfile check  [--env-file PATH]   ->  "ok: <n> keys"
    python -m deploy.envfile export [--env-file PATH]   ->  one "export KEY=<shell-quoted value>" line per key
PATH defaults to deploy/.env.local. A refused configuration prints "config error: <problem>" lines on stderr and
exits 2.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shlex
import sys

HERE = pathlib.Path(__file__).resolve().parent
EXAMPLE = HERE / ".env.example"  # the committed key list
LOCAL = HERE / ".env.local"  # the user's filled copy (git-ignored); the CLI default

_NAME = r"[a-z][a-z0-9-]{0,48}[a-z0-9]"
_NAME_RULE = (_NAME, "a lowercase name of 2 to 50 letters, digits or hyphens, starting with a letter and not ending "
                     "with a hyphen")
_PROJECT = r"[a-z][a-z0-9-]{4,28}[a-z0-9]"
_SA = r"[a-z][a-z0-9-]{4,28}[a-z0-9]@<PROJECT>\.iam\.gserviceaccount\.com"  # <PROJECT>: the configured PROJECT
_SA_RULE = (_SA, "<name>@<PROJECT>.iam.gserviceaccount.com for the configured PROJECT")

# Every key, in the example's order: (a regex the whole value must match, the rule in plain words).
RULES: dict[str, tuple[str, str]] = {
    "PROJECT": (_PROJECT, "a Google Cloud project ID: 6 to 30 lowercase letters, digits or hyphens, starting with a "
                          "letter and not ending with a hyphen"),
    "REGION": (r"[a-z]+-[a-z]+[0-9]", "a Google Cloud region such as asia-southeast1"),
    "GEMINI_MODEL": (r"gemini-[a-z0-9.-]+", "a Gemini model ID: gemini- then lowercase letters, digits, dots or "
                                           "hyphens"),
    "VERTEX_LOCATION": (r"[a-z]+-[a-z]+[0-9]|global|us|eu", "a region such as asia-southeast1, or global, us or eu"),
    "BILLING_ACCOUNT": (r"[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}", "a billing account ID: three groups of six digits or "
                                                               "capital letters A to F, joined by hyphens"),
    "CONTACT_EMAIL": (r"(?:[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)?", "empty or a plain email address"),
    "MIN_INSTANCES": (r"[01]", "0 or 1"),
    "GLOBAL_AI_CAP": (r"[1-9][0-9]{0,6}", "a whole number from 1 to 9999999, digits only"),
    "BUDGET_NAME": _NAME_RULE,
    "BUDGET_AMOUNT_USD": (r"[1-9][0-9]{0,5}(?:\.[0-9]{1,2})?", "a positive amount in US dollars, digits with at most "
                                                               "two decimals"),
    "AR_REPO": _NAME_RULE,
    "IMAGE_NAME": _NAME_RULE,
    "SERVICE": _NAME_RULE,
    "BUILD_JOB": _NAME_RULE,
    "BENCH_JOB": _NAME_RULE,
    "EARLY_BENCH_JOB": _NAME_RULE,
    "SQL_INSTANCE": _NAME_RULE,
    "DB_NAME": (r"[a-z_][a-z0-9_]{0,62}", "a database name: up to 63 lowercase letters, digits or underscores, not "
                                         "starting with a digit"),
    "RUN_SA_EMAIL": _SA_RULE,
    "DEPLOY_SA_EMAIL": _SA_RULE,
    "PASSCODE_SECRET": _NAME_RULE,
    "HQ_PASSCODE_SECRET": _NAME_RULE,
    "DATA_BUCKET": (r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]",
                    "a Cloud Storage bucket name: 3 to 63 lowercase letters, digits or hyphens, starting and ending "
                    "with a letter or digit"),
    "UPLOADS_BUCKET": (r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]",
                       "a Cloud Storage bucket name: 3 to 63 lowercase letters, digits or hyphens, starting and ending "
                       "with a letter or digit"),
    "RELEASE_DATA_DIR": (r"(?:[A-Za-z0-9._/-]+(?: [A-Za-z0-9._/-]+)*)?",
                         "empty, or the public artifact folder's path: letters, digits, '.', '_', '-', '/' and single "
                         "spaces"),
}
# The keys whose example value is a <placeholder>: everything but the decided public values (D-03, D-07) and
# RELEASE_DATA_DIR, a local folder that identifies no cloud resource and is empty in the example (quick 261017-v60).
IDENTIFYING = frozenset(RULES) - {"REGION", "GEMINI_MODEL", "VERTEX_LOCATION", "MIN_INSTANCES", "GLOBAL_AI_CAP",
                                  "RELEASE_DATA_DIR"}
# CONTACT_EMAIL may be empty until Phase 11's paused page; RELEASE_DATA_DIR is empty for a release that builds
# data/reference, as before the public set (quick 261017-v60)
OPTIONAL = frozenset({"CONTACT_EMAIL", "RELEASE_DATA_DIR"})

_ENTRY = re.compile(r"^([A-Z][A-Z0-9_]*)=([^'\"]*)$")
_KEYISH = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")  # names the key of a malformed line
_PLACEHOLDER = re.compile(r"<[^<>]*>")
_SUFFIX = ".gserviceaccount.com"


class ConfigError(ValueError):
    """A refused configuration; the message has one problem per line, each naming a key or a line, never a value."""


def parse(path: pathlib.Path | str) -> list[tuple[int, str, str]]:
    """[(line number, key, value)] of a configuration file that holds only comment lines, blank lines and plain
    KEY=value lines; any other line is a ConfigError."""
    path = pathlib.Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: cannot be read ({type(exc).__name__}); copy deploy/.env.example and fill it "
                          f"in") from None
    entries, problems = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ENTRY.match(line)
        if m:
            entries.append((n, m.group(1), m.group(2)))
            continue
        k = _KEYISH.match(line)
        if k is None:
            problems.append(f"line {n}: not a comment, a blank line or KEY=value")
        elif re.match(r"^[A-Z][A-Z0-9_]*=", line):  # the key and '=' are plain, so a quote made it fail
            problems.append(f"{k.group(1)} (line {n}): a quoted value; write {k.group(1)}=value without quotes")
        else:
            problems.append(f"{k.group(1)} (line {n}): not a plain KEY=value line (no export, no spaces, no quotes)")
    if problems:
        raise ConfigError("\n".join(problems))
    return entries


def load(local: pathlib.Path | str = LOCAL, example: pathlib.Path | str = EXAMPLE, *,
         allow_placeholders: bool = False) -> dict[str, str]:
    """{key: value} in the example's order, after every check above; ConfigError lists every problem.
    `allow_placeholders` accepts a <placeholder> for an IDENTIFYING key (to check the example itself)."""
    keys = [key for _, key, _ in parse(example)]
    if keys != list(RULES):
        raise ConfigError(f"{example}: its keys are not envfile.RULES' keys in order; fix the two together")
    entries = parse(local)
    # PROJECT first, wherever its line is: the service-account rules name it.
    project = next((value for _, key, value in entries if key == "PROJECT"), None)
    seen: dict[str, int] = {}
    values: dict[str, str] = {}
    problems = []
    for n, key, value in entries:
        if key in seen:
            problems.append(f"{key} (line {n}): duplicate key (first on line {seen[key]})")
            continue
        seen[key] = n
        if key not in RULES:
            problems.append(f"{key} (line {n}): unknown key (the keys are deploy/.env.example's)")
            continue
        if value == "" and key not in OPTIONAL:
            problems.append(f"{key} (line {n}): empty")
        elif _PLACEHOLDER.fullmatch(value):
            if not (allow_placeholders and key in IDENTIFYING):
                problems.append(f"{key} (line {n}): still holds a <placeholder>; fill in the real value")
        elif not re.fullmatch(_pattern(key, project), value):
            problems.append(f"{key} (line {n}): must be {RULES[key][1]}")
        values[key] = value
    hq, store = values.get("HQ_PASSCODE_SECRET"), values.get("PASSCODE_SECRET")
    if hq and store and hq == store:  # 06-REVIEW IN-05: keys only, never the name
        problems.append(f"HQ_PASSCODE_SECRET (line {seen['HQ_PASSCODE_SECRET']}): must name another secret than "
                        f"PASSCODE_SECRET")
    problems += [f"{key}: missing (every key in deploy/.env.example is required)" for key in RULES if key not in seen]
    if problems:
        raise ConfigError("\n".join(problems))
    return {key: values[key] for key in RULES}


def _pattern(key: str, project: str | None) -> str:
    """The key's rule; a service account's names the configured PROJECT (any valid project when PROJECT itself was
    refused, so only PROJECT's own problem is reported for it)."""
    pattern = RULES[key][0]
    if "<PROJECT>" in pattern:
        valid = project is not None and re.fullmatch(_PROJECT, project)
        pattern = pattern.replace("<PROJECT>", re.escape(project) if valid else _PROJECT)
    return pattern


def iam_db_user(email: str) -> str:
    """A service account's IAM database user: its email without .gserviceaccount.com (Cloud SQL IAM login)."""
    if not email.endswith(_SUFFIX):
        raise ConfigError("a service-account email ends with .gserviceaccount.com")
    return email[: -len(_SUFFIX)]


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", type=pathlib.Path, default=None,
                        help="the configuration to read (default deploy/.env.local)")
    ap = argparse.ArgumentParser(prog="python -m deploy.envfile", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("check", parents=[common], help="validate the configuration; print 'ok: <n> keys'")
    sub.add_parser("export", parents=[common], help="print one 'export KEY=<quoted value>' line per key")
    args = ap.parse_args(argv)
    try:
        values = load(args.env_file or LOCAL)  # LOCAL read at call time, so tests can point it elsewhere
    except ConfigError as exc:
        for problem in str(exc).splitlines():
            print(f"config error: {problem}", file=sys.stderr)
        return 2
    if args.command == "check":
        print(f"ok: {len(values)} keys")
    else:
        for key, value in values.items():
            print(f"export {key}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
