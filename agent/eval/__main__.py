"""Lazy dispatcher for the dated evaluation command contract."""
from importlib import import_module
import sys

COMMANDS = {
    "build-refs": ("agent.eval.refs:build_refs_main", "10-05"),
    "fingerprint": ("agent.eval.fingerprint:main", "10-05"),
    "approval-sheet": ("agent.eval.approval:approval_sheet_main", "10-19"),
    "run": ("agent.eval.runner:run_main", "10-07"),
    "export": ("agent.eval.cli:export_main", "10-04"),
    "import": ("agent.eval.cli:import_main", "10-04"),
    "publish": ("agent.eval.cli:publish_main", "10-04"),
    "preflight": ("agent.eval.cli:preflight_main", "10-15"),
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        print("usage: python -m agent.eval {" + ", ".join(COMMANDS) + "}", file=sys.stderr)
        return 2
    command = argv.pop(0)
    target, plan = COMMANDS[command]
    module_name, function = target.split(":")
    try:
        module = import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise  # an existing module's broken dependency is not an unbuilt command
        print(f"{command}: not built yet (plan {plan})", file=sys.stderr)
        return 2
    if not hasattr(module, function):
        print(f"{command}: not built yet (plan {plan})", file=sys.stderr)
        return 2
    try:
        return getattr(module, function)(argv)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
