"""Canonical JSON for EngineInput: one contract for version, cache and export (spec §6)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace

from .adjust import coerce_input
from .model import (Bookings, Capacity, EngineInput, Forecast, Inbound, Lot, MarkdownResponse, Policy, Product,
                    Run)


def _clean(value, places: int | None = None):
    """Tuples become lists and -0.0 becomes 0.0. With places, floats are also rounded: the hash path only."""
    if isinstance(value, float):
        if places is not None:
            value = round(value, places)
        return 0.0 if value == 0 else value  # no "-0.0"
    if isinstance(value, dict):
        return {k: _clean(v, places) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v, places) for v in value]
    return value


def normalized(inp: EngineInput) -> EngineInput:
    """coerce_input (adjust.py, the engine's one number boundary) plus lists in one defined order, so equal business
    inputs give equal JSON and rec_version hashes the numbers the engine computes on, to 6 decimal places (D-18 WR-07).
    canonical_json rounds floats to 6 places, so two inputs that differ below that share a version even if their
    cents differ (round-3 IN-01; Phase 4's cache keyed by rec_version accounts for it).

    Adjustments keep their order; coerce_input converts their numbers where exact.
    """
    inp = coerce_input(inp)
    b = inp.bookings
    return replace(
        inp,
        lots=tuple(sorted(inp.lots, key=lambda l: l.lot_id)),
        inbound=tuple(sorted(inp.inbound, key=lambda i: i.inbound_id)),
        runs=tuple(sorted(inp.runs, key=lambda r: r.run_code)),
        forecasts=tuple(sorted(inp.forecasts, key=lambda f: (f.branch, f.day))),
        capacity=tuple(sorted(inp.capacity, key=lambda c: (c.branch, c.day))),
        sales_yesterday=tuple(sorted(inp.sales_yesterday)),
        bookings=Bookings(tuple(sorted(b.demand)), tuple(sorted(b.totes)), tuple(sorted(b.run_totes)),
                          tuple(sorted(b.lot_units)), tuple(sorted(b.lot_price))),
        markdown_schedule=tuple(sorted(inp.markdown_schedule)),  # step order never changes the version (ESL-01)
    )


def to_dict(inp: EngineInput) -> dict:
    """The export payload: full float precision, so from_dict(to_dict(x)) replays to the exact same result."""
    d = _clean(asdict(normalized(inp)))
    d.pop("applied_overrides", None)  # records how an input was processed, not business data: never hashed
    return d


def canonical_json(inp: EngineInput) -> str:
    """The hashed form: floats rounded to 6 places, so float noise does not change rec_version."""
    return json.dumps(_clean(to_dict(inp), 6), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=float)


def rec_version(inp: EngineInput) -> str:
    return hashlib.sha256(canonical_json(inp).encode("utf-8")).hexdigest()


def _steps(value):
    """The schedule's JSON rows as tuples. A value that is not a list (a null, a str) or a step that is not a list
    is passed through unchanged, so validate_input names markdown_schedule (review WR-05), never a bare TypeError.
    A missing key is () (no schedule), as in exports made before the field existed."""
    if not isinstance(value, (list, tuple)):
        return value
    return tuple(tuple(x) if isinstance(x, (list, tuple)) else x for x in value)


def from_dict(d: dict) -> EngineInput:
    """Inverse of to_dict, used by export checks and the replay CLI."""
    b = d["bookings"]
    md = d["markdown"]
    return EngineInput(
        engine_version=d["engine_version"],
        today=d["today"],
        decision_min=d["decision_min"],
        case_lot_id=d["case_lot_id"],
        product=Product(**d["product"]),
        lots=tuple(Lot(**x) for x in d["lots"]),
        inbound=tuple(Inbound(**x) for x in d["inbound"]),
        runs=tuple(Run(**x) for x in d["runs"]),
        forecasts=tuple(Forecast(**x) for x in d["forecasts"]),
        capacity=tuple(Capacity(**x) for x in d["capacity"]),
        sales_yesterday=tuple((br, u) for br, u in d["sales_yesterday"]),
        markdown=MarkdownResponse(tuple(md["m20"]), tuple(md["m30"])),
        policy=Policy(**d["policy"]),
        bookings=Bookings(tuple(tuple(x) for x in b["demand"]), tuple(tuple(x) for x in b["totes"]),
                          tuple(tuple(x) for x in b["run_totes"]), tuple(tuple(x) for x in b["lot_units"]),
                          tuple(b["lot_price"])),
        reported_facts=tuple(d["reported_facts"]),
        overrides=tuple(d["overrides"]),
        markdown_schedule=_steps(d.get("markdown_schedule", ())),
    )


def export_bundle(inp: EngineInput, result: dict, **meta) -> dict:
    """The case export (spec §6 Export): the complete EngineInput, the result and the versions.

    `meta` carries what the engine doesn't know, e.g. dataset_version and forecast_run_id.
    """
    return {"engine_input": to_dict(inp), "rec_version": rec_version(inp),
            "engine_version": inp.engine_version, "result": result, **meta}
