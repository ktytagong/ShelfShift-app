"""The engine worker and the result cache (OPS-06; 04-API-CONTRACT "Decisions recorded here", IN-01).

The worker is on for this module (conftest turns it off for every other API test). Inputs are the engine's hand
fixtures F1 and F6, imported read-only; expected values come from the contract and the locked decisions, never from
engine output.
"""
from __future__ import annotations

import os
import signal
import time
from dataclasses import replace

import pytest

from api import config, db, evaluate, worker
from api.errors import ApiError, engine_error
from engine import Plan, check_plan, from_dict, rec_version, to_dict
from engine.adjust import AdjustmentError, InputError, effective
from engine.model import KEEP
from engine.simulate import HorizonError
from tests.engine.fixtures import SAT, f1, f6, with_overrides


# Module-level helpers, so the worker can unpickle them by name.
def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _check_keep(inp):
    return check_plan(inp, Plan(KEEP))


def _die() -> None:
    os._exit(1)


@pytest.fixture
def worker_on(monkeypatch):
    monkeypatch.setattr(config, "ENGINE_WORKER", True)
    worker.start()
    yield
    worker.stop()


@pytest.fixture
def empty_cache(schema):
    with db.tx() as conn:
        db.run(conn, "DELETE FROM engine_cache")
        db.run(conn, "DELETE FROM recommendations WHERE run_id LIKE 'test-worker-%'")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def gone_within(pid: int, seconds: float = 5.0) -> bool:
    """A killed process is reaped by its parent shortly after SIGKILL; poll for that, never a fixed sleep."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.02)
    return False


def cache_rows() -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, "SELECT cache_key, rec_version FROM engine_cache ORDER BY cache_key")


def test_a_search_runs_in_the_warm_worker_and_is_cached_by_its_full_input(worker_on, empty_cache, monkeypatch):
    inp = f1()
    assert worker.pid() is not None and worker.pid() != os.getpid()
    assert worker.run(os.getpid) == worker.pid()  # a call runs in the recorded, warm worker
    with db.tx() as conn:
        first = evaluate.run_engine(conn, inp)
    assert first[0] == rec_version(inp)
    assert cache_rows() == [{"cache_key": evaluate.cache_key(inp), "rec_version": rec_version(inp)}]

    def no_engine(*_):
        raise AssertionError("a cache hit must not reach the worker")

    monkeypatch.setattr(worker, "run", no_engine)
    with db.tx() as conn:
        assert evaluate.run_engine(conn, inp) == first


def test_two_inputs_equal_to_six_decimals_never_share_a_result(worker_on, empty_cache):
    a = f1()
    lot = a.lots[0]
    b = replace(a, lots=(replace(lot, qty=lot.qty + 1e-7),))  # differs in the seventh decimal
    assert rec_version(a) == rec_version(b)
    assert evaluate.cache_key(a) != evaluate.cache_key(b)
    with db.tx() as conn:
        va = evaluate.run_engine(conn, a)[0]
        vb = evaluate.run_engine(conn, b)[0]
    assert va == vb == rec_version(a)
    rows = cache_rows()
    assert sorted(r["cache_key"] for r in rows) == sorted([evaluate.cache_key(a), evaluate.cache_key(b)])
    assert {r["rec_version"] for r in rows} == {rec_version(a)}


def test_only_raw_inputs_are_stored(worker_on, empty_cache):
    raw = with_overrides(f1(), {"kind": "transport_quote", "run_code": "R-02", "fixed_cost": 500.0})
    eff = effective(raw)
    assert eff.applied_overrides == ("transport_quote",)
    with db.tx() as conn:
        with pytest.raises(ValueError, match="refusing to store an effective input"):
            evaluate.store(conn, "test-worker-raw", "SUS-1", 3, eff)
        with pytest.raises(ValueError, match="refusing to store an effective input"):
            evaluate.run_engine(conn, eff)
    assert cache_rows() == []
    with db.tx() as conn:
        version, _, _ = evaluate.store(conn, "test-worker-raw", "SUS-1", 3, raw)
        row = db.one(conn, "SELECT engine_input, overrides, is_whatif FROM recommendations "
                           "WHERE run_id = 'test-worker-raw' AND case_id = 'SUS-1'")
    assert version == rec_version(raw)
    assert row["engine_input"] == to_dict(raw)
    assert from_dict(row["engine_input"]).overrides == raw.overrides  # the override is kept raw, not applied
    assert row["overrides"] == [{"kind": "transport_quote", "run_code": "R-02", "fixed_cost": 500.0}]
    assert row["is_whatif"] is False


def test_the_worker_never_forks(worker_on):
    method = worker._pool._mp_context.get_start_method()
    assert method in ("forkserver", "spawn")
    assert worker._pool._max_workers == 1


def test_a_killed_worker_recovers_on_the_next_call(worker_on, empty_cache):
    old = worker.pid()
    os.kill(old, signal.SIGKILL)
    assert gone_within(old)
    with db.tx() as conn:
        version, result, _ = evaluate.run_engine(conn, f1())  # a cache miss: it must reach a worker
    assert version == rec_version(f1()) and result["engine_version"] == f1().engine_version
    assert worker.pid() not in (None, old, os.getpid())
    assert alive(worker.pid())


def test_a_stuck_call_is_stopped_after_the_timeout(worker_on, monkeypatch):
    old = worker.pid()
    monkeypatch.setattr(config, "ENGINE_TIMEOUT_S", 0.5)
    began = time.monotonic()
    with pytest.raises(ApiError) as e:
        worker.run(_sleep, 5)
    took = time.monotonic() - began
    assert (e.value.status, e.value.code) == (503, "engine_timeout")
    assert e.value.message == "The engine took too long and was restarted. Try again."
    assert 0.5 <= took < 5  # stopped at the timeout, never waited out
    assert worker.pid() is None and worker._pool is None
    assert gone_within(old)
    monkeypatch.setattr(config, "ENGINE_TIMEOUT_S", 15.0)
    assert worker.run(os.getpid) == worker.pid() not in (None, old)
    assert not worker._lock.locked()


def test_queue_wait_is_not_running_time(worker_on, monkeypatch):
    running = worker.pid()
    pool = worker._pool
    monkeypatch.setattr(config, "ENGINE_QUEUE_S", 0.2)
    assert worker._lock.acquire(timeout=1)  # another sandbox's search holds the worker
    try:
        with pytest.raises(ApiError) as e:
            worker.run(os.getpid)
    finally:
        worker._lock.release()
    assert (e.value.status, e.value.code) == (503, "engine_busy")
    assert e.value.message == "The engine is busy with another request. Try again in a few seconds."
    assert worker.pid() == running and worker._pool is pool and alive(running)  # the running job was left alone
    assert worker.run(os.getpid) == running


def test_a_worker_that_dies_twice_is_unavailable(worker_on):
    with pytest.raises(ApiError) as e:
        worker.run(_die)
    assert (e.value.status, e.value.code) == (503, "engine_unavailable")
    assert e.value.message == "The engine is unavailable right now. Try again in a moment."
    assert not worker._lock.locked()
    assert worker.run(os.getpid) not in (None, os.getpid())  # the next call gets a fresh worker


def test_engine_errors_cross_the_worker_as_the_same_class(worker_on):
    other_engine = replace(f1(), engine_version="engine-2")
    with pytest.raises(InputError) as e:
        evaluate.compute(other_engine)
    mapped = engine_error(e.value)
    assert (mapped.status, mapped.code, mapped.message) == (400, "bad_input", str(e.value))
    assert "engine_version" in mapped.message

    what_if = with_overrides(f1(), {"kind": "decide_at", "minute": 630})
    with pytest.raises(AdjustmentError) as e:
        worker.run(_check_keep, what_if)
    mapped = engine_error(e.value)
    assert (mapped.status, mapped.code, mapped.message) == (400, "bad_override", str(e.value))

    short = replace(f6(), forecasts=tuple(f for f in f6().forecasts if f.day < SAT))  # stops before the horizon
    with pytest.raises(HorizonError) as e:
        evaluate.compute(short)
    mapped = engine_error(e.value)
    assert (mapped.status, mapped.code, mapped.message) == (409, "missing_data", str(e.value))

    for exc in (InputError("x"), AdjustmentError("x"), HorizonError("x")):
        assert engine_error(exc).code not in ("conflict", "incomplete_shipment")
    assert engine_error(ValueError("x")) is None and engine_error(KeyError("x")) is None


def test_inline_mode_runs_without_a_worker(monkeypatch):
    monkeypatch.setattr(config, "ENGINE_WORKER", False)
    worker.stop()
    inline = evaluate.compute(f1())
    assert worker._pool is None and worker.pid() is None
    monkeypatch.setattr(config, "ENGINE_WORKER", True)
    try:
        on_worker = evaluate.compute(f1())
        assert worker.pid() not in (None, os.getpid())
    finally:
        worker.stop()
    assert on_worker == inline
