"""A filled deploy configuration with obviously synthetic values, for tests and dry runs (06-CONTEXT D-03, D-08).

`write(path)` copies deploy/.env.example and replaces each <placeholder> with its value from SYNTHETIC, so the
result passes `deploy.envfile.load`. Every value is fake on its face: the project is x-demo-12, the billing account
is all zeros, and every resource name starts with demo. This helper never reads deploy/.env.local. `DRILL` holds the
five names a drill configuration marks (deploy/release.py is_drill; deploy/README.md step 11), as overrides. A drill
shares DATA_BUCKET with the live configuration (its objects sit under the dataset version and are read-only to every
job), so DRILL marks the same five names (quick 261017-v60).

CLI: `python -m tests.deploy.synthetic_env OUT [--drill]` writes OUT (with --drill, the drill configuration).
"""
from __future__ import annotations

import pathlib
import re
import sys

from deploy import envfile

# Every key of envfile.IDENTIFYING (the keys whose example value is a placeholder), with a synthetic value.
SYNTHETIC = {
    "PROJECT": "x-demo-12",
    "BILLING_ACCOUNT": "000000-000000-000000",
    "CONTACT_EMAIL": "judge+demo@example.com",  # synthetic; a release refuses an empty one (11 D-04)
    "BUDGET_NAME": "demo-budget",
    "BUDGET_AMOUNT_USD": "12",
    "AR_REPO": "demo-repo",
    "IMAGE_NAME": "demo-image",
    "SERVICE": "demo-svc",
    "BUILD_JOB": "demo-build",
    "BENCH_JOB": "demo-bench",
    "EARLY_BENCH_JOB": "demo-early",
    "SQL_INSTANCE": "demo-sql",
    "DB_NAME": "demo_db",
    "RUN_SA_EMAIL": "run-sa@x-demo-12.iam.gserviceaccount.com",
    "DEPLOY_SA_EMAIL": "deploy-sa@x-demo-12.iam.gserviceaccount.com",
    "PASSCODE_SECRET": "demo-pass",
    "HQ_PASSCODE_SECRET": "demo-hq-pass",
    "DATA_BUCKET": "demo-data",
    "UPLOADS_BUCKET": "demo-uploads",
}

# The drill configuration's five marked names: SERVICE and the three jobs end -drill, DB_NAME ends _drill.
DRILL = {
    "SERVICE": "demo-svc-drill",
    "BUILD_JOB": "demo-build-drill",
    "BENCH_JOB": "demo-bench-drill",
    "EARLY_BENCH_JOB": "demo-early-drill",
    "DB_NAME": "demo_db_drill",
}

_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
_PLACEHOLDER = re.compile(r"<[^<>]*>")


def write(path: pathlib.Path | str, example: pathlib.Path | str = envfile.EXAMPLE, **overrides: str) -> pathlib.Path:
    """Write the example to `path` with every placeholder replaced by its SYNTHETIC value. A keyword override
    replaces that key's value whatever the example holds (tests use it to write one bad or boundary value)."""
    out = []
    for line in pathlib.Path(example).read_text(encoding="utf-8").splitlines():
        m = _LINE.match(line)
        if m and m.group(1) in overrides:
            line = f"{m.group(1)}={overrides[m.group(1)]}"
        elif m and _PLACEHOLDER.fullmatch(m.group(2)):
            line = f"{m.group(1)}={SYNTHETIC[m.group(1)]}"
        out.append(line)
    path = pathlib.Path(path)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (1, 2) or (len(args) == 2 and args[1] != "--drill"):
        print("usage: python -m tests.deploy.synthetic_env OUT [--drill]", file=sys.stderr)
        return 2
    write(args[0], **(DRILL if len(args) == 2 else {}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
