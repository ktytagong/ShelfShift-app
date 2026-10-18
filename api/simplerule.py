"""The case page's three-way comparison: Recommended, Simple rule and Keep (06.1-CONTEXT D-27, D-43; 06.1-RESEARCH
Pattern 2; 04-API-CONTRACT "Product mode (06.1)" Comparison).

Composition in api/, never an engine rule. `compose(inp, result)` takes the input a search ran on and that search's
result, and scores three plans with the engine's own functions on the same effective input: the recommended plan
(the result's key), the Simple rule (`engine/baseline.py` `baseline_plan`: the store's markdowns plus the earliest
passing transfer of the surplus) and Keep. Each column is `engine/search.py` `_row` for that plan, so when a column's
plan is one of the result's options its figures are that option row's, key for key; beside it are `transport` (the
run's quote plus handling) and `moved` (units shipped), low/base/high, from the same simulation. `evaluate()`'s result
gains no key and `ENGINE_VERSION` stays as it is (D-43).

It runs only in the engine worker, beside the search (`evaluate._compute`), or on a cache hit stored before 06.1
inside a write (`evaluate.run_engine`); a read never computes it. A missing forecast day is
`{"status": "missing_data", "reason": <the engine's message>, "columns": []}`, never a made-up figure; any other engine
error propagates. Module-level, so the worker can unpickle it by name; it imports the engine only.

`parse_plan` (an engine plan key back to a Plan) lives here so the worker needs no API module; `cases.parse_plan` is
this function.
"""
from __future__ import annotations

from urllib.parse import unquote

from engine import baseline
from engine.adjust import coerce_input, effective
from engine.model import KEEP, MARKDOWN, SCENARIOS, TRANSFER, EngineInput, Plan
from engine.scoring import cents, score
from engine.search import _row
from engine.simulate import HorizonError

OK, MISSING_DATA, NOT_COMPUTED = "ok", "missing_data", "not_computed"
COLUMNS = ("recommended", "simple_rule", "keep")  # the contract's column order


def parse_plan(key: str) -> Plan:
    """The engine's plan key back to a Plan (engine/model.py Plan.key); a URL-quoted key is accepted."""
    key = unquote(key)
    if key == "keep":
        return Plan(KEEP)
    if key.startswith("md"):
        return Plan(MARKDOWN, int(key[2:]))
    _, dest, run_code, qty, rest = key.split(":")
    return Plan(TRANSFER, 0 if rest == "hold" else int(rest[2:]), int(qty), dest, run_code)


def empty(status: str, reason: str | None = None) -> dict:
    """A comparison with no columns: `missing_data` (with the engine's reason) or `not_computed` (stored before 06.1,
    until its next write). No figure is ever filled in for it."""
    return {"status": status, "reason": reason, "columns": []}


def compose(inp: EngineInput, result: dict) -> dict:
    """{"status": "ok", "reason": None, "columns": [recommended, simple_rule, keep]} for the search `result` of `inp`,
    or `missing_data` when the simple rule or a simulation lacks a forecast day (HorizonError)."""
    eff = effective(coerce_input(inp))  # the input evaluate() computes on (engine/search.py)
    try:
        plans = {"recommended": parse_plan(result["recommended"]), "simple_rule": baseline.baseline_plan(inp),
                 "keep": Plan(KEEP)}
        scored = {sc.plan.key: sc for sc in score(eff, list({p.key: p for p in plans.values()}.values()))}
    except HorizonError as exc:
        return empty(MISSING_DATA, str(exc))
    keep = scored[Plan(KEEP).key]

    def column(name: str) -> dict:
        sc = scored[plans[name].key]
        return {**_row(sc, keep, eff.product),
                "transport": {s: cents(sc.sims[s].transport + sc.sims[s].handling) for s in SCENARIOS},
                "moved": {s: round(sc.sims[s].moved, 2) + 0.0 for s in SCENARIOS},
                "column": name}
    return {"status": OK, "reason": None, "columns": [column(name) for name in COLUMNS]}
