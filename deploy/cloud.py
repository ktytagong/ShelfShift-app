"""The one way the deploy scripts talk to Google Cloud (OPS-08; 06-CONTEXT D-04, D-07, D-24).

The user runs these scripts in their own terminal; Claude never does. Claude only runs them with --dry-run and a
synthetic configuration (tests.deploy.synthetic_env), which needs no gcloud and runs nothing.

    gcloud(*args, project=...)  ->  ["gcloud", *args, "--project", project]: every command names its project
    Runner                      ->  runs commands with subprocess; prints each one as "+ <command>" first
    DryRunner                   ->  prints and records each command and runs nothing; answers from scripted rules

Standard input (the passcode, D-24) is never printed, recorded or put in an error: a command that takes stdin is
shown with "< (withheld)". A value passed to `hide` (the billing account, D-04) is printed as its label, in the echoed
command and in an error's stderr, though the command itself still carries it. A failed command raises CommandError
with the command's first words, its exit code and the last lines of its stderr. It also keeps the whole masked stderr
(`stderr`), for matching only: an answer such as gcloud's `Image not found.` can sit above a later notice that pushes it
out of the tail. That whole stderr is never printed, recorded or put in the error's message.
"""
from __future__ import annotations

import json as _json
import shlex
import subprocess
import sys
from typing import Any, Callable

WITHHELD = "(withheld)"  # what is printed and recorded in place of a command's standard input
TAIL_LINES = 5  # how many stderr lines a CommandError keeps

Predicate = Callable[[list[str]], bool]


def first_words(argv: list[str], limit: int = 4) -> str:
    """The command's leading words before its first flag, at most `limit`: enough to name it in an error."""
    lead = []
    for word in argv:
        if word.startswith("-") or len(lead) == limit:
            break
        lead.append(word)
    return " ".join(lead)


class CommandError(RuntimeError):
    """A command that exited non-zero (or could not start). Holds no standard input. `stderr` is the whole masked
    stderr (default: the tail), for matching only; it is never printed or recorded, and the message holds the tail."""

    def __init__(self, argv0: str, returncode: int, stderr_tail: str, stderr: str | None = None):
        self.argv0, self.returncode, self.stderr_tail = argv0, returncode, stderr_tail
        self.stderr = stderr_tail if stderr is None else stderr
        detail = f": {stderr_tail}" if stderr_tail else ""
        super().__init__(f"{argv0} failed (exit {returncode}){detail}")


def gcloud(*args: str, project: str) -> list[str]:
    """A gcloud argv that names its project explicitly, never the gcloud config's default (D-04)."""
    return ["gcloud", *args, "--project", project]


class Runner:
    """Runs commands for real, printing each first. `out` defaults to sys.stdout at print time."""

    def __init__(self, out=None):
        self.out = out
        self.hidden: dict[str, str] = {}  # value -> the label printed in its place

    def hide(self, value: str, label: str) -> None:
        """Never print `value`: echoed commands and error tails show `label` instead."""
        if value:
            self.hidden[value] = label

    def mask(self, text: str) -> str:
        for value, label in self.hidden.items():
            text = text.replace(value, label)
        return text

    def _echo(self, argv: list[str], stdin: str | None) -> None:
        line = "+ " + shlex.join(argv) + (f" < {WITHHELD}" if stdin is not None else "")
        print(self.mask(line), file=self.out or sys.stdout, flush=True)

    def _exec(self, argv: list[str], stdin: str | None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(argv, input=stdin, capture_output=True, text=True)
        except FileNotFoundError:
            return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: command not found")

    def run(self, argv: list[str], *, stdin: str | None = None) -> str:
        """Run `argv` (with `stdin` on its standard input); its stdout, or CommandError when it exits non-zero."""
        argv = list(argv)
        self._echo(argv, stdin)
        p = self._exec(argv, stdin)
        if p.returncode != 0:
            whole = (p.stderr or "").strip()
            tail = "\n".join(whole.splitlines()[-TAIL_LINES:])
            raise CommandError(self.mask(first_words(argv)), p.returncode, self.mask(tail), stderr=self.mask(whole))
        return p.stdout

    def ok(self, argv: list[str]) -> bool:
        """True when `argv` exits 0 (a describe: does it exist?); its output is discarded."""
        argv = list(argv)
        self._echo(argv, None)
        return self._exec(argv, None).returncode == 0

    def json(self, argv: list[str]) -> Any:
        """`argv` run with --format=json, its stdout parsed."""
        return _json.loads(self.run(list(argv) + ["--format=json"]) or "null")


class DryRunner(Runner):
    """Prints and records each command and runs nothing (the only way Claude runs the setup scripts).

    `rules` is a list of (predicate on argv, response); the first match answers the call. Without a match, run
    returns "", ok returns False (nothing exists yet) and json returns None. A command matching `fail` raises
    CommandError (exit 1), after it is printed and recorded."""

    def __init__(self, out=None, rules: list[tuple[Predicate, Any]] | None = None, fail: Predicate | None = None):
        super().__init__(out)
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []  # WITHHELD where the command took standard input, else None
        self.rules = list(rules or [])
        self.fail = fail

    def _answer(self, argv: list[str], stdin: str | None, default: Any) -> Any:
        self._echo(argv, stdin)
        self.calls.append(list(argv))
        self.inputs.append(WITHHELD if stdin is not None else None)
        if self.fail is not None and self.fail(argv):
            raise CommandError(self.mask(first_words(argv)), 1, "(dry run: scripted failure)")
        for predicate, response in self.rules:
            if predicate(argv):
                return response
        return default

    def run(self, argv: list[str], *, stdin: str | None = None) -> str:
        return self._answer(list(argv), stdin, "")

    def ok(self, argv: list[str]) -> bool:
        return self._answer(list(argv), None, False)

    def json(self, argv: list[str]) -> Any:
        return self._answer(list(argv) + ["--format=json"], None, None)
