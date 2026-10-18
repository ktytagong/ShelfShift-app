"""The one engine worker process (OPS-06): engine searches run here, so a search never blocks a page load.

One `ProcessPoolExecutor(max_workers=1)` on the forkserver start method (spawn where forkserver is missing; never
fork, which is unsafe in a threaded server). Its initializer imports the engine, and `start` (the app's lifespan)
makes a first call that records the worker's pid, so no request pays for the spawn.

Only one job is in flight. A caller waits for the process-local lock for at most `config.ENGINE_QUEUE_S` and then
gets 503 engine_busy without touching the pool, so a caller that waited never kills the job that is running; the
running call's own timeout measures running time only (04-RESEARCH Pitfall 1).

A running call waits at most `config.ENGINE_TIMEOUT_S` (api/settings.json, well above the 3 s case budget so a slow
cold start is not killed). On timeout the worker is killed with SIGKILL, the pool is dropped and that request gets
503 engine_timeout; the next call starts a fresh worker. Python 3.12 has no public terminate (`terminate_workers`
arrives in 3.14), so `_discard` kills the recorded pid itself. A dead worker (BrokenProcessPool) is replaced and the
call retried once; a second failure is 503 engine_unavailable. Engine errors raised in the worker cross back as the
same classes (ValueError subclasses with one argument) for `errors.engine_error` to map.
"""
from __future__ import annotations

import concurrent.futures as cf
import multiprocessing as mp
import os
import signal
import threading
from concurrent.futures.process import BrokenProcessPool

from . import config
from .errors import ApiError

_lock = threading.Lock()
_pool: cf.ProcessPoolExecutor | None = None
_pid: int | None = None

# Messages exactly as the contract's error table (04-API-CONTRACT "Errors").
BUSY = "The engine is busy with another request. Try again in a few seconds."
TIMEOUT = "The engine took too long and was restarted. Try again."
UNAVAILABLE = "The engine is unavailable right now. Try again in a moment."


def _context():
    """forkserver where the platform has it, else spawn; never fork (a threaded parent)."""
    return mp.get_context("forkserver" if "forkserver" in mp.get_all_start_methods() else "spawn")


def _import_engine() -> None:
    import engine  # noqa: F401  (a module-level function, so it is importable under spawn and forkserver)


def _start() -> None:
    """Build the pool and warm it: the first call records the worker's pid. Callers hold the lock."""
    global _pool, _pid
    _pool = cf.ProcessPoolExecutor(max_workers=1, mp_context=_context(), initializer=_import_engine)
    _pid = _pool.submit(os.getpid).result(timeout=config.ENGINE_TIMEOUT_S)


def _discard() -> None:
    """Kill the worker (SIGKILL to the recorded pid, and to any pool process, e.g. one stuck before its warm-up
    call returned a pid) and drop the pool without waiting. Callers hold the lock."""
    global _pool, _pid
    pids = {_pid} | set(getattr(_pool, "_processes", None) or ())
    for p in pids - {None}:
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
    _pool, _pid = None, None


def start() -> None:
    """The app's lifespan startup: a warm worker before the first request (a no-op when one is running)."""
    if not _lock.acquire(timeout=config.ENGINE_QUEUE_S):
        raise ApiError(503, "engine_busy", BUSY)
    try:
        if _pool is None:
            _start()
    finally:
        _lock.release()


def stop() -> None:
    """The app's lifespan shutdown: the pool is shut down and forgotten."""
    global _pool, _pid
    if not _lock.acquire(timeout=config.ENGINE_QUEUE_S):
        raise ApiError(503, "engine_busy", BUSY)
    try:
        if _pool is not None:
            _pool.shutdown(wait=True, cancel_futures=True)
        _pool, _pid = None, None
    finally:
        _lock.release()


def pid() -> int | None:
    """The running worker's pid, recorded by the warm-up call (None when no worker is running)."""
    return _pid


def run(fn, *args):
    """fn(*args) in the worker; fn must be a module-level function (pickled by name). Inline when ENGINE_WORKER is
    off. One job in flight: the lock is held around submit and result, and released in every case."""
    if not config.ENGINE_WORKER:
        return fn(*args)
    if not _lock.acquire(timeout=config.ENGINE_QUEUE_S):
        raise ApiError(503, "engine_busy", BUSY)  # queue time: the running job and the pool are left alone
    try:
        for attempt in (1, 2):
            try:
                if _pool is None:
                    _start()
                return _pool.submit(fn, *args).result(timeout=config.ENGINE_TIMEOUT_S)
            except BrokenProcessPool:
                _discard()
                if attempt == 2:
                    raise ApiError(503, "engine_unavailable", UNAVAILABLE) from None
            except cf.TimeoutError:
                _discard()
                raise ApiError(503, "engine_timeout", TIMEOUT) from None
    finally:
        _lock.release()
