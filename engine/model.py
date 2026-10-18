"""Engine data types.

Days are demo-day indexes (Day 3 = Thu 15 Oct 2026). Times are minutes after
midnight on that day. Quantities are floats because expected sales can be
fractional; plan quantities are whole units.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ENGINE_VERSION = "engine-3"

KEEP, MARKDOWN, TRANSFER = "KEEP", "MARKDOWN", "TRANSFER"
SCENARIOS = ("low", "base", "high")
MOVE_RANK = {KEEP: 0, MARKDOWN: 1, TRANSFER: 2}


@dataclass(frozen=True)
class Product:
    sku: str
    zone: str  # "chilled" | "ambient"
    price: float
    disposal_cost: float
    units_per_tote: int
    kg_per_unit: float


@dataclass(frozen=True)
class Lot:
    lot_id: str
    branch: str
    qty: float
    expiry_day: int  # sellable until the end of this day
    received_seq: int  # FEFO tie-break: lower sells first
    markdown_pct: int = 0
    expiry_source: str = "PRINTED"  # PRINTED | ESTIMATED
    last_verified_day: int | None = None


@dataclass(frozen=True)
class Inbound:
    inbound_id: str
    branch: str
    qty: float
    arrival_day: int
    arrival_min: int
    expiry_day: int
    received_seq: int


@dataclass(frozen=True)
class Run:
    run_code: str
    origin: str
    dest: str
    day: int
    loading_cutoff_min: int
    departs_min: int
    arrives_min: int  # may exceed 1440 after a delay: the arrival is on a later day
    spare_totes: int
    cold_chain: bool
    fixed_cost: float  # the quote, one trip
    per_unit_cost: float  # handling per unit moved
    available: bool = True
    cancel_cost: float | None = None  # paid if a dispatch ships nothing; None = the full quote. An assumption, not observed


@dataclass(frozen=True)
class Forecast:
    branch: str
    day: int
    low: float
    base: float
    high: float


@dataclass(frozen=True)
class Capacity:
    branch: str
    day: int
    free_totes: int  # in the product's zone


# Round-3 IN-03, rev 6.3.0: engine-2's retailer thresholds, kept under their engine-2 names (01-CONTEXT D-20).
# They are only the defaults of the Policy fields below; engine logic reads the fields, and Phase 3 seeds them.
STALE_AFTER_DAYS = 7
STALE_FACTOR = 0.7
PHANTOM_MIN_FORECAST = 10.0
NEAR_EXPIRY_DAYS = 1  # stores usually mark down 30% when a lot expires today or tomorrow


@dataclass(frozen=True)
class Policy:
    packing_min: int = 120
    min_sellable_days: int = 1
    relabel_min: int = 60
    markdown_same_day_by_min: int = 720  # prepare + relabel at or before 12:00: same day
    sellable_same_day_before_min: int = 840  # arrival before 14:00: sellable that day
    # Money guards, in the settings currency (D-14, spec §6 EngineInput). The defaults are the fixtures' values.
    min_regret: float = 1.0  # money guard: a smaller regret doesn't count as changing the plan
    crossover_range_floor: float = 20_000.0  # money guard: the transport crossover range is at least [0, this]
    adjustment_cost_max: float = 100_000.0  # money guard: the upper bound on a money value in an adjustment
    # Round-3 IN-03, rev 6.3.0: retailer thresholds, seeded in Phase 3. The defaults are the engine-2 constants above.
    stale_after_days: int = STALE_AFTER_DAYS  # a count older than this many days is stale
    stale_factor: float = STALE_FACTOR  # the share of the record a stale count is tested at
    phantom_min_forecast: float = PHANTOM_MIN_FORECAST  # base forecast today at or above which zero sales flag phantom stock
    baseline_near_expiry_days: int = NEAR_EXPIRY_DAYS  # the baseline marks down when the lot expires within this many days
    baseline_markdown_pct: int = 30  # the baseline's near-expiry markdown, 20 or 30
    # Rev 6.3.0 (ENG-10): money per kg wasted, in the settings currency. It changes only the ranking score
    # (net - waste_price x kg wasted), never a net figure. A retailer policy value, not a cash cost.
    waste_price: float = 0.0
    # Money guard (D-05): the waste flip price range is [0, this]; 180 = $5/kg x 36 in fixture money; the USD seed
    # sets $5/kg.
    waste_flip_max: float = 180.0
    # Rev 6.3.0 (ENG-09): 0 = full search; 5 or 10 = transfer quantities on that step plus each run's cap. Set in
    # reference data, never switched automatically.
    shortlist_step: int = 0


@dataclass(frozen=True)
class MarkdownResponse:
    m20: tuple[float, float, float]  # low, base, high assumption
    m30: tuple[float, float, float]


@dataclass(frozen=True)
class Bookings:
    """Quantities held or committed by OTHER cases. No IDs, no expiry times."""

    demand: tuple[tuple[str, int, float], ...] = ()  # (branch, day, units)
    totes: tuple[tuple[str, int, int], ...] = ()  # (branch, day, totes)
    run_totes: tuple[tuple[str, int], ...] = ()  # (run_code, totes)
    lot_units: tuple[tuple[str, float], ...] = ()  # (lot_id, units set aside)
    lot_price: tuple[str, ...] = ()  # lot_ids whose price right is taken


@dataclass(frozen=True)
class EngineInput:
    """Everything one case evaluation uses, and nothing else (spec §6)."""

    engine_version: str
    today: int
    decision_min: int
    case_lot_id: str
    product: Product
    lots: tuple[Lot, ...]  # every lot of this SKU at the source and candidate destinations
    inbound: tuple[Inbound, ...]
    runs: tuple[Run, ...]  # runs leaving the source branch
    forecasts: tuple[Forecast, ...]
    capacity: tuple[Capacity, ...]
    sales_yesterday: tuple[tuple[str, float], ...]  # (branch, recorded units)
    markdown: MarkdownResponse
    policy: Policy = Policy()
    bookings: Bookings = Bookings()
    reported_facts: tuple[dict, ...] = ()  # confirmed field signals, applied first
    overrides: tuple[dict, ...] = ()  # what-ifs and adopted assumptions, applied last
    # the kinds of the overrides effective() applied, never a reported fact's kind. Not compared and not hashed;
    # check_plan refuses an input whose list holds a what-if-only kind (D-18 WR-01)
    applied_overrides: tuple[str, ...] = field(default=(), compare=False)
    # (days before expiry, pct off) steps of the product's automatic markdown schedule (rev 6.3.0, ESL-01);
    # () = no schedule. The last field, so positional construction stays valid
    markdown_schedule: tuple[tuple[int, int], ...] = ()

    def case_lot(self) -> Lot:
        return next(l for l in self.lots if l.lot_id == self.case_lot_id)

    @property
    def source(self) -> str:
        return self.case_lot().branch


@dataclass(frozen=True)
class Plan:
    kind: str  # KEEP | MARKDOWN | TRANSFER
    pct: int = 0  # MARKDOWN: 20 or 30. TRANSFER: the remainder (0 = hold, 20, 30)
    qty: int = 0
    dest: str | None = None
    run_code: str | None = None

    @property
    def key(self) -> str:
        if ":" in f"{self.dest}{self.run_code}":
            raise ValueError(f"':' is not allowed in branch names or run codes: {self.dest!r} {self.run_code!r}")
        if self.kind == KEEP:
            return "keep"
        if self.kind == MARKDOWN:
            return f"md{self.pct}"
        rest = "hold" if self.pct == 0 else f"md{self.pct}"
        return f"tr:{self.dest}:{self.run_code}:{self.qty}:{rest}"

    @property
    def label(self) -> str:
        if self.kind == KEEP:
            return "Keep"
        if self.kind == MARKDOWN:
            return f"Markdown {self.pct}%"
        rest = "hold rest" if self.pct == 0 else f"markdown rest {self.pct}%"
        return f"Transfer {self.qty} to {self.dest} ({self.run_code}), {rest}"


def totals(rows) -> dict:
    """Sum (key, value) rows by key. Two bookings in one bucket add up; they never overwrite each other."""
    out: dict = {}
    for key, value in rows:
        out[key] = out.get(key, 0) + value
    return out


def trip_cost(fixed_cost: float, cancel_cost: float | None, per_unit_cost: float, shipped: float) -> tuple[float, float]:
    """(transport, handling) for one dispatch. `simulate` and `world.step_day` both use it, so forecast and outcome agree.

    Anything shipped: the full quote, plus handling per unit. Nothing shipped: the cancel cost
    (None = the full quote) and no handling.
    """
    if shipped > 1e-9:
        return fixed_cost, per_unit_cost * shipped
    return (fixed_cost if cancel_cost is None else cancel_cost), 0.0


def scheduled_pct(schedule, expiry_day: int, day: int) -> int:
    """The markdown schedule's percent off on `day` for a lot sellable through `expiry_day` (0 = full price).

    A step (k, pct) applies from opening on day expiry_day - k. Percents are off the full price and never compound:
    the deepest step in force wins. `simulate`, `world.step_day`, the baseline and the case result all use it, so
    forecast and outcome agree (spec §6 Markdown schedule, rev 6.3.0).
    """
    return max((pct for k, pct in schedule if day >= expiry_day - k), default=0)


def reduced_schedule(schedule) -> tuple[tuple[int, int], ...]:
    """The schedule's effective steps: for each percent, the step with the largest k, sorted by k descending (D-26).

    A step (k, pct) applies from opening on day expiry_day - k, so among the steps of one percent the largest k
    starts first and is in force on every day the others are; percents never compound (`scheduled_pct` takes the
    deepest in force), so the other steps of that percent never change a price. With levels 20/30 only and the pct
    never falling toward expiry, `scheduled_pct` gives the same percent on every day for the full and the reduced
    schedule, and the earliest day at each percent (the covered reason's day, `markdown_step`, `act_before`) is
    kept. Apply it only to a schedule that has passed `schedule_problem`; `adjust.effective` does so once.
    """
    deepest: dict[int, int] = {}
    for k, pct in schedule:
        if k > deepest.get(pct, -1):
            deepest[pct] = k
    return tuple(sorted(((k, pct) for pct, k in deepest.items()), reverse=True))


def schedule_problem(schedule) -> str | None:
    """None when `schedule` is a valid markdown schedule, else a short reason. The one rule for validation and the
    world step (spec §6 Markdown schedule, rev 6.3.0).

    Checked in order: the schedule is a tuple or list (None or a str is a reason, never a TypeError; review WR-05);
    each step is a 2-item tuple or list of whole numbers (an int that is not a bool); k >= 0; pct is 20 or 30
    (D-08); each k appears once; going from the largest k to the smallest (toward expiry) the pct never falls.
    """
    if not isinstance(schedule, (tuple, list)):
        return "must be a tuple of (days before expiry, pct) steps; () = no schedule"
    for step in schedule:
        if not isinstance(step, (tuple, list)) or len(step) != 2 or not all(
                isinstance(v, int) and not isinstance(v, bool) for v in step):
            return f"step {step!r} must be (days before expiry, pct) whole numbers"
    for k, pct in schedule:
        if k < 0:
            return f"step ({k}, {pct}): days before expiry must be >= 0"
        if pct not in (20, 30):
            return f"step ({k}, {pct}): pct must be 20 or 30"
    ks = [k for k, _ in schedule]
    if len(set(ks)) != len(ks):
        return "each days-before-expiry value must appear once"
    pcts = [pct for _, pct in sorted(schedule, reverse=True)]
    if any(later < earlier for earlier, later in zip(pcts, pcts[1:])):
        return "the pct must never fall toward expiry"
    return None
