"""The one entry point that reads the FreshRetailNet-50K files (05.2-CONTEXT D-06, D-07).

Run it from the ShelfShift-web root with the data environment and isolated mode:

    .venv-data/bin/python -I tools/frn.py verify --raw public-data/raw
    .venv-data/bin/python -I tools/frn.py profile --raw public-data/raw --out data/reference/reports/public-profile.json

Why -I: the dataset is untrusted bytes from the internet. Isolated mode keeps PYTHON* environment variables, the user
site-packages and the current folder off sys.path, so nothing next to the data or in the shell can be imported by
accident. This script then puts only the repo root on sys.path itself. The files live in public-data/raw/ (git-ignored,
outside the importable data/ package) and are read, never executed or unpickled. pyarrow exists only in .venv-data and
is imported only inside the parquet reader.

COMMANDS names every pipeline subcommand of Phase 05.2. Each module is imported only when its subcommand runs. A
subcommand whose module does not exist yet prints "<name>: not available yet" and exits 2. Later plans add the
modules and never edit this file.
"""
from __future__ import annotations

import importlib
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COMMANDS = {
    "verify": "data.public:main",
    "profile": "data.public:main",
    "measure": "data.measure:main",
    "stock": "data.public_build:main",
    "network": "data.public_build:main",
    "build": "data.public_build:main",
    "check": "data.manifest:main",
    "forecast": "data.public_forecast:main",
    "backtest": "data.public_forecast:main",
    "assumptions": "data.public_config:main",
}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: frn.py {{{','.join(COMMANDS)}}} ...", file=sys.stderr)
        return 2
    name = argv[0]
    module_name, function_name = COMMANDS[name].split(":")
    function = None
    if importlib.util.find_spec(module_name) is not None:
        function = getattr(importlib.import_module(module_name), function_name, None)
    if function is None:
        print(f"{name}: not available yet", file=sys.stderr)
        return 2
    return function(argv)  # the subcommand's own parser reads its name first


if __name__ == "__main__":
    sys.exit(main())
