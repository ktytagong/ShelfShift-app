"""The local candidate network per source store (spec §5 Public dataset, local candidate network; DATA-14; 05.2 D-21,
D-29, D-30, D-31).

The dataset has no locations, and proximity is never inferred from a store or city code (D-30). Positions come only
from an assumed mapping: each store of the mapping gets a point drawn, from a stream keyed by its city and store code,
uniformly inside a disc of `city_radius_km`. Only distances inside one city are ever used, so a city's centre is
irrelevant. The city code limits candidates to the same city, because the dataset card defines `city_id` as an
encoded city ID (D-30); the code is compared for equality only, never read as a number.

Stage 1, its lot-independent part (D-29): a destination is a candidate for a source store when it is in the same city,
both stores are in the mapping, and the travel time is within `max_travel_min` (equal is in). Each source keeps its k
nearest candidates, sorted by (travel minutes, store code), and gets one run to each on every run day, so at most k
runs leave a store per day (D-21: the case budget grows fast with destinations). A store missing from the mapping, or
listed in `unmapped`, gets no pair and no invented route; it is listed, and keeps every other function, capacity
included (D-31).

Transport cost is never a filter: the cost parameters only fill the cost fields of the run rows, and the engine weighs
them against keep and markdown (D-29). The lot-dependent eligibility (arrival before expiry, cold chain against the
product's zone, receiving capacity on the day) is stage 1's second half and lives in the API at case time (05.2-14).

The run and capacity rows carry the keys `data.seed` writes for `reference/world.json`, so `api.refdata` and
`api.inputs` read them unchanged (`travel_min` is one extra key). Every draw is keyed by a string (the scheme of
`data.seed._rng`), so the output is the same under every PYTHONHASHSEED; no clock is read. No retailer value is a code
literal: every value is a NetworkParams field the caller fills from reference data (05.2-07).
"""
from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from engine.allocator import EPS

MINUTES_PER_HOUR = 60
CENT_PLACES = 2  # money is rounded to cents


def _rng(kind: str, seed: int, *key) -> random.Random:
    """A draw stream keyed by a string, so it is the same under every PYTHONHASHSEED (as data.seed._rng)."""
    return random.Random(f"{kind}:{seed}:" + ":".join(map(str, key)))


@dataclass(frozen=True, kw_only=True)
class NetworkParams:
    """Every value of the network; the caller fills each from reference data, each an Assumption (05.2-07)."""

    city_radius_km: float  # the assumed mapping: stores sit inside a disc of this radius per city
    speed_kmh: float
    handling_min: int  # minutes added to every trip (loading, unloading)
    max_travel_min: int  # the service limit: a destination at exactly this travel time is eligible
    k: int  # at most k destinations per source store (one run each per run day)
    run_days: tuple[int, ...]
    loading_cutoff_min: int
    departs_min: int
    spare_totes: int
    cold_chain_share: float  # share of runs with a cold chain, drawn per (origin, dest)
    fixed_cost_base: float  # the quote: base + per_km_cost × km
    per_km_cost: float
    per_unit_cost: float
    cancel_cost: float | None  # None = the full quote (spec §5)
    capacity_totes: Mapping[str, int]  # zone -> free totes per store per day
    unmapped: frozenset  # store codes the mapping leaves out (D-31)
    seed: int


def positions(stores_by_city: Mapping[str, Iterable[str]], params: NetworkParams) -> dict[str, tuple[float, float]]:
    """{store: (x_km, y_km)} for every store of the mapping: a point uniform in the city's disc, keyed by (city,
    store). Stores in `params.unmapped` get none."""
    out = {}
    for city in sorted(stores_by_city):
        for store in sorted(stores_by_city[city]):
            if store in params.unmapped:
                continue
            rng = _rng("pos", params.seed, city, store)
            r = params.city_radius_km * math.sqrt(rng.random())
            theta = math.tau * rng.random()
            out[store] = (r * math.cos(theta), r * math.sin(theta))
    return out


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def travel_min(a: tuple[float, float], b: tuple[float, float], params: NetworkParams) -> int:
    """Travel minutes between two positions: euclidean km ÷ speed × 60 + handling, rounded up to a whole minute
    (float noise under EPS never adds a minute)."""
    if not params.speed_kmh > 0:
        raise ValueError(f"speed_kmh must be above 0, got {params.speed_kmh!r}")
    return math.ceil(_km(a, b) * MINUTES_PER_HOUR / params.speed_kmh + params.handling_min - EPS)


def candidate_pairs(stores_by_city: Mapping[str, Iterable[str]], pos: Mapping[str, tuple[float, float]],
                    params: NetworkParams) -> tuple[list[dict], list[str]]:
    """(pairs, unmapped). pairs: per source store (cities and stores in code order), its at most k nearest same-city
    destinations within the service limit, sorted by (travel_min, code), each {origin, dest, city, km, travel_min,
    rank}. unmapped: the sorted store codes with no position, which get no pair as source or destination (D-31)."""
    if not isinstance(params.k, int) or isinstance(params.k, bool) or params.k < 0:
        raise ValueError(f"k must be a whole number >= 0, got {params.k!r}")
    pairs, unmapped = [], []
    for city in sorted(stores_by_city):
        stores = sorted(stores_by_city[city])
        mapped = [s for s in stores if s in pos and s not in params.unmapped]
        unmapped.extend(s for s in stores if s not in mapped)
        for origin in mapped:
            near = sorted((travel_min(pos[origin], pos[dest], params), dest) for dest in mapped if dest != origin)
            near = [(t, dest) for t, dest in near if t <= params.max_travel_min][:params.k]
            for rank, (t, dest) in enumerate(near):
                pairs.append({"origin": origin, "dest": dest, "city": city, "km": _km(pos[origin], pos[dest]),
                              "travel_min": t, "rank": rank})
    return pairs, sorted(unmapped)


def run_rows(pairs: Iterable[dict], params: NetworkParams) -> list[dict]:
    """One run row per pair per run day, in (run_code, day) order, with the keys data.seed writes plus travel_min.
    Cost fields come from the cost parameters only; they never decide which rows exist (D-29)."""
    rows = []
    for pair in pairs:
        origin, dest = pair["origin"], pair["dest"]
        cold = _rng("cold", params.seed, origin, dest).random() < params.cold_chain_share
        fixed = round(params.fixed_cost_base + params.per_km_cost * pair["km"], CENT_PLACES)
        for day in params.run_days:
            rows.append({"run_code": f"R-{origin}-{dest}", "day": day, "origin": origin, "dest": dest,
                         "loading_cutoff_min": params.loading_cutoff_min, "departs_min": params.departs_min,
                         "arrives_min": params.departs_min + pair["travel_min"], "spare_totes": params.spare_totes,
                         "cold_chain": cold, "fixed_cost": fixed, "per_unit_cost": params.per_unit_cost,
                         "cancel_cost": params.cancel_cost, "travel_min": pair["travel_min"]})
    return sorted(rows, key=lambda r: (r["run_code"], r["day"]))


def capacity_rows(stores: Iterable[str], zones: Iterable[str], days: Iterable[int],
                  params: NetworkParams) -> list[dict]:
    """One row per store, zone and day: the zone's free totes. An unmapped store keeps its capacity (D-31)."""
    zones, days = list(zones), list(days)
    for zone in zones:
        if zone not in params.capacity_totes:
            raise ValueError(f"zone {zone!r} has no capacity_totes value")
    return sorted(({"branch": store, "zone": zone, "day": day, "free_totes": params.capacity_totes[zone]}
                   for store in set(stores) for zone in zones for day in days),
                  key=lambda r: (r["branch"], r["zone"], r["day"]))
