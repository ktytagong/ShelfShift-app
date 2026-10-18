"""Run the engine for an EngineInput: one worker process, a shared cache keyed by the exact input (spec §4, §6).

The cache key is the sha256 of the full-precision `to_dict` input, so two inputs a cent apart never share a cached
result; `rec_version` (a 6-decimal hash) stays the version label shown to clients and the stale-version check
(04-API-CONTRACT "Decisions recorded here", round-3 IN-01). Only raw inputs are cached and stored: `to_dict` drops
`applied_overrides`, so storing an input on which `effective()` already applied a what-if would let a round trip
reopen the engine's what-if gate (contract-diff rows 26, 27). Hits and misses return the same JSON form.

The case page's three-way comparison (06.1 D-27, D-43; api/simplerule.py) is computed in the worker beside the search,
cached in `engine_cache.simple_rule` under the same key and copied to the version row (`recommendations.simple_rule`).
A cache row stored before 06.1 has none: the next write that reaches it (an evaluation, an adopt, a what-if) computes
it through the worker and fills the row; a read never does (OPS-06). `evaluate()`'s result gains no key.
"""
from __future__ import annotations

import hashlib
import json

from engine import evaluate, fact_checks, from_dict, rec_version, to_dict
from engine.model import EngineInput

from . import db, simplerule, worker


def _compute(inp: EngineInput) -> tuple[dict, list[dict], dict]:
    """The search, its fact checks and the three-way comparison (06.1 D-27). Module-level, so the worker can unpickle
    it by name."""
    result = evaluate(inp)
    return result, fact_checks(inp, result), simplerule.compose(inp, result)


def compute(inp: EngineInput) -> tuple[dict, list[dict], dict]:
    """Every search goes through the single worker (inline only when ENGINE_WORKER is off)."""
    return worker.run(_compute, inp)


def cache_key(inp: EngineInput) -> str:
    """sha256 of the full-precision to_dict (engine/canonical.py); rec_version stays the label."""
    body = json.dumps(to_dict(inp), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _require_raw(inp: EngineInput) -> None:
    if inp.applied_overrides:
        raise ValueError("refusing to store an effective input: overrides were already applied "
                         f"({', '.join(inp.applied_overrides)}); pass the raw input")


def run_engine(conn, inp: EngineInput) -> tuple[str, dict, list[dict]]:
    """(rec_version, result, fact checks). The same exact input in any sandbox shares one cached result, and its
    comparison: a miss stores both; a hit whose row has no comparison yet (stored before 06.1) gets it computed in the
    worker and written to that row, so the version row stored next can copy it (`comparison_for`)."""
    _require_raw(inp)
    inp = from_dict(to_dict(inp))  # the engine runs on exactly the stored input, so an export replays to the cent
    key, version = cache_key(inp), rec_version(inp)
    hit = db.one(conn, "SELECT result, fact_checks, simple_rule FROM engine_cache WHERE cache_key = :k", k=key)
    if hit:
        if hit["simple_rule"] is None:
            comparison = worker.run(simplerule.compose, inp, hit["result"])
            db.run(conn, "UPDATE engine_cache SET simple_rule = CAST(:s AS jsonb) WHERE cache_key = :k "
                         "AND simple_rule IS NULL", k=key, s=db.js(comparison))
        return version, hit["result"], hit["fact_checks"]
    result, checks, comparison = compute(inp)
    result, checks = json.loads(db.js(result)), json.loads(db.js(checks))  # the form a cache hit returns
    db.run(conn, "INSERT INTO engine_cache (cache_key, rec_version, result, fact_checks, simple_rule) "
                 "VALUES (:k, :v, CAST(:r AS jsonb), CAST(:f AS jsonb), CAST(:s AS jsonb)) ON CONFLICT DO NOTHING",
           k=key, v=version, r=db.js(result), f=db.js(checks), s=db.js(comparison))
    return version, result, checks


def comparison_for(conn, inp: EngineInput) -> dict:
    """The cached comparison for this exact raw input (the row `run_engine` wrote or filled), or `not_computed` when
    the cache has none. Reads only; never calls the worker."""
    key = cache_key(from_dict(to_dict(inp)))  # the key run_engine used (it normalises the input the same way)
    row = db.one(conn, "SELECT simple_rule FROM engine_cache WHERE cache_key = :k", k=key)
    return row["simple_rule"] if row and row["simple_rule"] is not None else simplerule.empty(simplerule.NOT_COMPUTED)


def store(conn, run_id: str, case_id: str, day: int, inp: EngineInput, whatif: bool = False,
          overrides: tuple[dict, ...] = (), note: str | None = None) -> tuple[str, dict, list[dict]]:
    """Evaluate and keep the version with the raw EngineInput it came from.

    `overrides` is what the recommendations row lists as this version's overrides (a what-if's own); when empty,
    the input's overrides are listed. A version label already stored for the case is left as it is (the label is
    the stale-version check), with one exception (WR-06): a real version (not a what-if) takes over a what-if row
    with the same label, so the version in force is never marked as a simulation and keeps its source note."""
    _require_raw(inp)
    version, result, checks = run_engine(conn, inp)
    comparison = comparison_for(conn, inp)
    kept = None if comparison["status"] == simplerule.NOT_COMPUTED else db.js(comparison)  # null stays fillable
    listed = list(overrides) if overrides else list(inp.overrides)
    db.run(conn, "INSERT INTO recommendations (run_id, case_id, rec_version, day, engine_input, result, fact_checks, "
                 "is_whatif, overrides, source_note, simple_rule) VALUES (:r, :c, :v, :d, CAST(:i AS jsonb), "
                 "CAST(:res AS jsonb), CAST(:f AS jsonb), :w, CAST(:o AS jsonb), :n, CAST(:s AS jsonb)) "
                 "ON CONFLICT (run_id, case_id, rec_version) DO UPDATE "
                 "SET is_whatif = false, day = EXCLUDED.day, engine_input = EXCLUDED.engine_input, "
                 "result = EXCLUDED.result, fact_checks = EXCLUDED.fact_checks, overrides = EXCLUDED.overrides, "
                 "source_note = coalesce(EXCLUDED.source_note, recommendations.source_note), "
                 "simple_rule = coalesce(EXCLUDED.simple_rule, recommendations.simple_rule) "
                 "WHERE recommendations.is_whatif AND NOT EXCLUDED.is_whatif",
           r=run_id, c=case_id, v=version, d=day, i=db.js(to_dict(inp)), res=db.js(result), f=db.js(checks),
           w=whatif, o=db.js(listed), n=note, s=kept)
    if kept is not None:  # a version row stored before 06.1 (same label) gets the comparison at this write
        db.run(conn, "UPDATE recommendations SET simple_rule = CAST(:s AS jsonb) WHERE run_id = :r AND case_id = :c "
                     "AND rec_version = :v AND simple_rule IS NULL", s=kept, r=run_id, c=case_id, v=version)
    return version, result, checks
