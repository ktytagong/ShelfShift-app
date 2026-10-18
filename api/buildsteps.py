"""One log line per template build step (quick 261018-4nj).

`with step(run, name):` around a step of `sandbox.build_template` or `advance.advance_day` prints one flushed line on
stdout when the step ends, whether it returned or raised:

    step d<day> <name>: <seconds> s, <statements> statements

<day> is the run's day when the step started, <seconds> the step's wall time with one decimal, and <statements> the
database statements this thread sent during it; a nested step's statements also count in the step around it. A line
is printed only for a template build's run (a TEMPLATE run that is still INITIALIZING). A workspace's Next day, a
route and an ACTIVE template print nothing.

Why: the deploy job's log is the only place a cloud build's time can be broken down. These lines show which step
the time goes to without a profiler, about 40 of them per build, all before the `template <id> ACTIVE for <dv>` line.
A line holds a day number, a step name, seconds and a count. It never holds a run id, a project, a resource name or
a credential.

Statements are counted by one class-level `before_cursor_execute` listener on SQLAlchemy's Engine, registered on the
first step that prints; it only adds one to a per-thread counter.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

import sqlalchemy as sa

_local = threading.local()
_lock = threading.Lock()
_listening = False


def _on_statement(conn, cursor, statement, parameters, context, executemany) -> None:
    _local.sent = getattr(_local, "sent", 0) + 1


def _listen() -> None:
    global _listening
    with _lock:
        if not _listening:
            sa.event.listen(sa.engine.Engine, "before_cursor_execute", _on_statement)
            _listening = True


def prints(run: dict | None) -> bool:
    """Whether a step of this run prints its line: only a template build's run (TEMPLATE, INITIALIZING)."""
    return bool(run) and run.get("kind") == "TEMPLATE" and run.get("status") == "INITIALIZING"


@contextmanager
def step(run: dict | None, name: str):
    """Time one build step and print its line on exit (see the module docstring); a no-op for any other run."""
    if not prints(run):
        yield
        return
    _listen()
    day = run.get("day")
    start, sent = time.perf_counter(), getattr(_local, "sent", 0)
    try:
        yield
    finally:
        seconds = time.perf_counter() - start
        n = getattr(_local, "sent", 0) - sent
        print(f"step d{day} {name}: {seconds:.1f} s, {n} statements", flush=True)
