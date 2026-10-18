"""Hand-calculated fixtures F1-F6 (spec §6). Written from the spec, never regenerated from the engine."""
from __future__ import annotations

from dataclasses import replace

from engine.model import (ENGINE_VERSION, Capacity, EngineInput, Forecast, Lot, MarkdownResponse, Product, Run)

THU, FRI, SAT = 3, 4, 5
BANG_NA, LAT_PHRAO = "Bang Na", "Lat Phrao"
MILK = Product(sku="MILK-1L", zone="chilled", price=40.0, disposal_cost=2.0, units_per_tote=10, kg_per_unit=1.03)
R02 = Run(run_code="R-02", origin=BANG_NA, dest=LAT_PHRAO, day=THU, loading_cutoff_min=720, departs_min=750,
          arrives_min=800, spare_totes=22, cold_chain=True, fixed_cost=600.0, per_unit_cost=0.0)


def _flat(branch: str, by_day: dict[int, float]) -> list[Forecast]:
    return [Forecast(branch, d, v, v, v) for d, v in by_day.items()]  # low = base = high in the fixtures


def f1() -> EngineInput:
    """F1: milk. Bang Na 500 units expiring end of Sat, counted this morning; Lat Phrao empty;
    R-02 at 600 including handling."""
    return EngineInput(
        engine_version=ENGINE_VERSION,
        today=THU,
        decision_min=9 * 60,
        case_lot_id="MLK-042",
        product=MILK,
        lots=(Lot("MLK-042", BANG_NA, 500.0, SAT, received_seq=1, last_verified_day=THU),),
        inbound=(),
        runs=(R02,),
        forecasts=tuple(_flat(BANG_NA, {THU: 100, FRI: 100, SAT: 100}) + _flat(LAT_PHRAO, {THU: 40, FRI: 50, SAT: 50})),
        capacity=tuple(Capacity(LAT_PHRAO, d, 16) for d in (THU, FRI, SAT)),
        sales_yesterday=((BANG_NA, 100.0), (LAT_PHRAO, 40.0)),
        markdown=MarkdownResponse(m20=(1.30, 43 / 30, 1.55), m30=(1.40, 1.6, 1.80)),
    )


def f6() -> EngineInput:
    """F6: F1 plus a recorded Lat Phrao lot of 60 (expiring Sat, received first, zero sales yesterday).
    Physically Lat Phrao has 0: that truth lives outside the engine input."""
    base = f1()
    lp = Lot("MLK-038", LAT_PHRAO, 60.0, SAT, received_seq=0, last_verified_day=THU - 8)
    return replace(base, lots=base.lots + (lp,), sales_yesterday=((BANG_NA, 100.0), (LAT_PHRAO, 0.0)))


def with_overrides(inp: EngineInput, *overrides: dict) -> EngineInput:
    return replace(inp, overrides=inp.overrides + tuple(overrides))


def with_facts(inp: EngineInput, *facts: dict) -> EngineInput:
    return replace(inp, reported_facts=inp.reported_facts + tuple(facts))
