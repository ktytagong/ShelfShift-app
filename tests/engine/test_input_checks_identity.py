"""coerce_input and validate_input against their f694682 versions, kept here verbatim as the oracle (quick 261017-tps).

The fast paths in engine/adjust.py (one coerce per screen, exact-type rows and dataclasses returned as they are) must
return or raise exactly what the old code did: the same type-exact value, or the same exception type, message and
cause type. The comparison is type-exact (a float by its hex, an int by type and value, containers by type), so a
bool, Decimal or subclass that the old path rejects or converts is never accepted by value. DB-free.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import replace
from decimal import Decimal

import pytest

import engine.canonical
from engine.adjust import InputError, coerce_input, validate_input
from engine.model import (ENGINE_VERSION, Bookings, Capacity, EngineInput, Forecast, Inbound, Lot, MarkdownResponse,
                          Policy, Product, Run, schedule_problem)
from tests.engine.fixtures import BANG_NA, FRI, LAT_PHRAO, SAT, THU, f1, f6, with_facts, with_overrides

# ---- ORACLE: engine/adjust.py at f6946820a7c4bfafff1218b61c38cf8db2181fec, lines 15-41 and 60-363, copied verbatim
# ---- with an old_ prefix on every copied name. Never edit this block.

old_MONEY = "MONEY"  # bound marker: 0 to the input's policy.adjustment_cost_max (D-14)
old_EXPIRY_DAY = "EXPIRY_DAY"  # bound marker: yesterday (a confirmed label may show an expired lot) to the last forecast day
old_COUNTED_DAY = "COUNTED_DAY"  # bound marker: any day up to today; a count can't come from a later day (D-18 WR-02)
old_MULT = (0.5, 3.0)
old_HOURS = (0.0, 24.0)
old_UNITS = (0.0, 100_000.0)

# kind -> {field: (type, bounds: a (low, high) tuple, a bound marker or None)}
# Day ranges (capacity, demand_multiplier) have no horizon bound (D-16). A range changes the input's days
# max(day_from, today)..day_to, so a range that has ended or lies wholly in the future changes nothing and stays in
# the input and its version. day_from <= day_to is still checked.
old_KINDS: dict[str, dict[str, tuple[type, tuple[float, float] | str | None]]] = {
    "run_available": {"run_code": (str, None), "value": (bool, None)},
    "cold_chain": {"run_code": (str, None), "value": (bool, None)},
    "decide_at": {"minute": (int, (0, 1439))},
    "departure_delay_h": {"run_code": (str, None), "hours": (float, old_HOURS)},
    "capacity": {"branch": (str, None), "day_from": (int, None), "day_to": (int, None),
                 "free_totes": (int, (0, 10_000))},
    "transport_quote": {"run_code": (str, None), "fixed_cost": (float, old_MONEY)},
    "handling_cost": {"run_code": (str, None), "per_unit_cost": (float, old_MONEY)},
    "markdown_response": {"m20": (float, old_MULT), "m30": (float, old_MULT)},
    "demand_multiplier": {"branch": (str, None), "day_from": (int, None), "day_to": (int, None),
                          "value": (float, old_MULT)},
    "disposal_cost": {"value": (float, old_MONEY)},
    "stock_count": {"lot_id": (str, None), "units": (float, old_UNITS), "counted_day": (int, old_COUNTED_DAY)},
    "expiry": {"lot_id": (str, None), "expiry_day": (int, old_EXPIRY_DAY)},
}

old_MARKDOWN_PCTS = (0, 20, 30)

# Declared number types, field by field (spec §6 EngineInput, "Version"): 500 and 500.0 must hash alike.
old_FLOAT_FIELDS = {
    Product: ("price", "disposal_cost", "kg_per_unit"),
    Lot: ("qty",),
    Inbound: ("qty",),
    Run: ("fixed_cost", "per_unit_cost", "cancel_cost"),
    Forecast: ("low", "base", "high"),
    Capacity: (),
    Policy: ("min_regret", "crossover_range_floor", "adjustment_cost_max", "stale_factor", "phantom_min_forecast",
             "waste_price", "waste_flip_max"),
}
old_INT_FIELDS = {
    Product: ("units_per_tote",),
    Lot: ("expiry_day", "received_seq", "markdown_pct", "last_verified_day"),
    Inbound: ("arrival_day", "arrival_min", "expiry_day", "received_seq"),
    Run: ("day", "loading_cutoff_min", "departs_min", "arrives_min", "spare_totes"),
    Forecast: ("day",),
    Capacity: ("day", "free_totes"),
    Policy: ("packing_min", "min_sellable_days", "relabel_min", "markdown_same_day_by_min",
             "sellable_same_day_before_min", "stale_after_days", "baseline_near_expiry_days", "baseline_markdown_pct",
             "shortlist_step"),
}
# the only declared numbers that may be None
old_NONE_ALLOWED = {(Lot, "last_verified_day"), (Run, "cancel_cost")}


def old__is_decimal(value) -> bool:
    """A Decimal-like number (pg8000 returns NUMERIC as Decimal), detected without the decimal module."""
    return not isinstance(value, (bool, int, float, str)) and hasattr(value, "as_integer_ratio")


def old__float(name: str, value) -> float:
    """Float-typed field: an int, float or Decimal-like becomes a float. A bool, NaN, a str or None is an InputError."""
    if isinstance(value, bool):
        raise InputError(f"{name}={value!r} must be a number, not a bool")
    if isinstance(value, (int, float)) or old__is_decimal(value):
        try:
            out = float(value)
        except (ValueError, OverflowError) as exc:  # e.g. a signalling NaN or a huge int
            raise InputError(f"{name}={value!r} must be a number") from exc
        if math.isnan(out):
            raise InputError(f"{name}={value!r} must not be NaN")
        return out
    raise InputError(f"{name}={value!r} must be a number")


def old__int(name: str, value) -> int:
    """Int-typed field: an int is kept; a float or Decimal-like becomes an int only when it is a whole number.

    A bool, a fraction, NaN, infinity, a str or None is an InputError.
    """
    if isinstance(value, bool):
        raise InputError(f"{name}={value!r} must be a whole number, not a bool")
    if isinstance(value, int):
        return value
    if isinstance(value, float) or old__is_decimal(value):
        try:
            num, den = value.as_integer_ratio()
        except (ValueError, OverflowError) as exc:  # NaN or infinity
            raise InputError(f"{name}={value!r} must be a whole number") from exc
        if den != 1:
            raise InputError(f"{name}={value!r} must be a whole number")
        return num
    raise InputError(f"{name}={value!r} must be a whole number")


def old__typed(obj, label: str):
    """Rebuild one dataclass with its numbers coerced by declared type. Messages read '<label><field>=...'."""
    cls = type(obj)

    def coerce(name: str, convert):
        value = getattr(obj, name)
        if value is None and (cls, name) in old_NONE_ALLOWED:
            return None
        return convert(f"{label}{name}", value)

    changes = {name: coerce(name, old__float) for name in old_FLOAT_FIELDS[cls]}
    changes.update({name: coerce(name, old__int) for name in old_INT_FIELDS[cls]})
    return replace(obj, **changes)


def old__row(row, types: str, label: str) -> tuple:
    """Coerce one tuple row position by position: 'f' float, 'i' int, '-' unchanged.

    A row that is not a tuple or list, or has a different number of values than types, is an InputError naming
    the field, never a bare ValueError or IndexError from unpacking it later (round-3 IN-02)."""
    if not isinstance(row, (tuple, list)) or len(row) != len(types):
        raise InputError(f"{label} must have {len(types)} values, got {row!r}")
    convert = {"f": old__float, "i": old__int, "-": lambda name, v: v}
    return tuple(convert[t](f"{label}[{i}]", v) for i, (t, v) in enumerate(zip(types, row)))


def old__payload(adj):
    """Convert an adjustment's numbers by KINDS field types, only where exact. Everything else, and unknown kinds and
    keys, pass through unchanged: validate names them with AdjustmentError."""
    kind = adj.get("kind") if isinstance(adj, dict) else None
    spec = old_KINDS.get(kind) if isinstance(kind, str) else None
    if spec is None:
        return adj
    out = {}
    for key, value in adj.items():
        typ = spec[key][0] if key in spec else None
        if isinstance(value, bool):
            pass
        elif typ is float and (isinstance(value, (int, float)) or old__is_decimal(value)):
            try:
                as_float = float(value)
            except (ValueError, OverflowError):
                as_float = math.nan
            if not math.isnan(as_float):
                value = as_float
        elif typ is int and (isinstance(value, float) or old__is_decimal(value)):
            try:
                num, den = value.as_integer_ratio()
            except (ValueError, OverflowError):
                den = 0
            if den == 1:
                value = num
        out[key] = value
    return out


def old_coerce_input(inp: EngineInput) -> EngineInput:
    """The engine's one number boundary (spec §6 EngineInput, D-18 WR-07).

    Every declared number is coerced exactly to its declared type, or rejected with InputError naming the field:
    a bool, a str, NaN, a fraction in an int field, or a None where None isn't allowed. Adjustment payload numbers
    are converted only where exact; the rest is left for validate. Nothing is sorted (canonical.normalized sorts),
    and applied_overrides is copied.
    """
    b = inp.bookings
    md = inp.markdown
    schedule = inp.markdown_schedule
    if not isinstance(schedule, (tuple, list)):  # None, a str such as "2:20 1:30", a dict (review WR-05)
        raise InputError(f"markdown_schedule={schedule!r} must be a tuple of (days before expiry, pct) steps; "
                         "() = no schedule")
    return replace(
        inp,
        today=old__int("today", inp.today),
        decision_min=old__int("decision_min", inp.decision_min),
        product=old__typed(inp.product, "product."),
        lots=tuple(old__typed(l, f"lot {l.lot_id} ") for l in inp.lots),
        inbound=tuple(old__typed(i, f"inbound {i.inbound_id} ") for i in inp.inbound),
        runs=tuple(old__typed(r, f"run {r.run_code} ") for r in inp.runs),
        forecasts=tuple(old__typed(f, f"forecast {f.branch} day {f.day} ") for f in inp.forecasts),
        capacity=tuple(old__typed(c, f"capacity {c.branch} day {c.day} ") for c in inp.capacity),
        sales_yesterday=tuple(old__row(r, "-f", f"sales_yesterday {r[0]}" if isinstance(r, (tuple, list)) and r
                                   else "sales_yesterday") for r in inp.sales_yesterday),
        markdown=MarkdownResponse(old__row(md.m20, "fff", "markdown.m20"), old__row(md.m30, "fff", "markdown.m30")),
        policy=old__typed(inp.policy, "policy."),
        bookings=Bookings(tuple(old__row(r, "-if", "bookings.demand") for r in b.demand),
                          tuple(old__row(r, "-ii", "bookings.totes") for r in b.totes),
                          tuple(old__row(r, "-i", "bookings.run_totes") for r in b.run_totes),
                          tuple(old__row(r, "-f", "bookings.lot_units") for r in b.lot_units),
                          tuple(b.lot_price)),
        reported_facts=tuple(old__payload(a) for a in inp.reported_facts),
        overrides=tuple(old__payload(a) for a in inp.overrides),
        applied_overrides=tuple(inp.applied_overrides),
        markdown_schedule=tuple(old__row(r, "ii", f"markdown_schedule[{i}]") for i, r in enumerate(schedule)),
    )


def old__fits(value) -> bool:
    """True when value is >= 0 and its fourth power (v x v x v x v) is a finite float (review WR-08, D-30).

    The engine multiplies at most three input magnitudes (waste price x kg per unit x units; price x units; per-unit
    cost x units) and sums the products over rows. A fourth power that is finite bounds each product by v^3 <= about
    1.5e231, which leaves room for any row count, so no computed net or kg can reach infinity. The limit (about
    1.158e77) is the float type's own range, not a value anyone chose. An int too large for a float, NaN and infinity
    fail.
    """
    try:
        f = float(value)
    except OverflowError:  # a whole number too large for a float
        return False
    return f >= 0 and math.isfinite(f * f * f * f)


def old__shown(value, too_large: str = "a whole number too large to print") -> str:
    """The value as given, or a fixed phrase for an int Python refuses to print (review WR-10, IN-12).

    CPython refuses int-to-str past sys.get_int_max_str_digits() digits: 4,300 by default, as low as 640 under
    -X int_max_str_digits or PYTHONINTMAXSTRDIGITS. Catching that refusal, never predicting the limit, keeps a message
    about a huge int an InputError naming its field.
    """
    try:
        return repr(value)
    except ValueError:  # an int past sys.get_int_max_str_digits()
        return too_large


def old__quantity(name: str, value: float) -> None:
    if not old__fits(value):
        # the value as given; an int Python refuses to print reads 'a whole number too large for a float', which is
        # always true of it (review IN-12: catch the refusal, never predict the limit)
        shown = old__shown(value, too_large="a whole number too large for a float")
        # review WR-08 (D-30): the float-range rule; review IN-09 (D-34): the message names no review and prints the
        # value as given
        raise InputError(f"{name}={shown} must be finite, >= 0 and at most about 1.158e77 (the engine's float range)")


def old_validate_input(inp: EngineInput) -> None:
    """The engine's input contract (spec §6, rev 6.2.1): checked at every public entry point.

    It checks the input as coerced by coerce_input (D-18 WR-07), so a number that can't be coerced exactly is an
    InputError too. Raises InputError naming the field, the id and the value. Never clamps or repairs. Every
    adjustment is bounded, so effective() of a valid input is valid too.

    Checks: the engine version; the markdown triples; units per tote; lot markdown percentages; every quantity,
    forecast, capacity, spare tote, sales and booking value finite and >= 0; unique lot ids and run codes; the
    Policy money guards and time fields, product price, disposal cost and kg per unit, and run fixed, per-unit and
    cancel costs finite and >= 0; the Policy min_sellable_days at least 1 (02-CONTEXT D-33); decision_min in
    0..1439; case_lot_id one of the lots (D-18 WR-06); the Policy
    stale_after_days, phantom_min_forecast and baseline_near_expiry_days finite and >= 0, stale_factor in 0..1,
    and baseline_markdown_pct 20 or 30 (round-3 IN-03); the Policy waste_price finite and >= 0 (rev 6.3.0, ENG-10);
    the Policy waste_flip_max finite and >= 0 (D-05), never compared with the waste price; the Policy shortlist_step
    0, 5 or 10 (rev 6.3.0, ENG-09); the markdown_schedule's steps whole numbers with k >= 0, pct 20 or 30, each k
    once and the pct never falling toward expiry (model.schedule_problem; rev 6.3.0, ESL-01, D-08); and every money
    value and quantity checked here small enough for the engine's arithmetic, its fourth power a finite float (about
    1.158e77, the float range; _fits, review WR-08, D-30), so no accepted input can overflow a net or a kg.
    """
    inp = old_coerce_input(inp)
    if inp.engine_version != ENGINE_VERSION:
        raise InputError(f"engine_version {inp.engine_version!r} is not this engine's {ENGINE_VERSION!r}")
    for name in ("m20", "m30"):
        low, base, high = values = getattr(inp.markdown, name)
        if not all(old_MULT[0] <= v <= old_MULT[1] for v in values):
            raise InputError(f"markdown.{name}={values} outside {old_MULT}")
        if not low <= base <= high:
            raise InputError(f"markdown.{name}={values} must have low <= base <= high")
    if inp.product.units_per_tote < 1:
        raise InputError(f"product.units_per_tote={inp.product.units_per_tote} must be >= 1")
    old__quantity("product.units_per_tote", inp.product.units_per_tote)  # review WR-08: the float range too
    for l in inp.lots:
        if l.markdown_pct not in old_MARKDOWN_PCTS:
            raise InputError(f"lot {l.lot_id} markdown_pct={l.markdown_pct} not in {old_MARKDOWN_PCTS}")
        old__quantity(f"lot {l.lot_id} qty", l.qty)
    for i in inp.inbound:
        old__quantity(f"inbound {i.inbound_id} qty", i.qty)
    for f in inp.forecasts:
        for scenario in ("low", "base", "high"):
            old__quantity(f"forecast {f.branch} day {f.day} {scenario}", getattr(f, scenario))
    for c in inp.capacity:
        old__quantity(f"capacity {c.branch} day {c.day} free_totes", c.free_totes)
    for r in inp.runs:
        old__quantity(f"run {r.run_code} spare_totes", r.spare_totes)
    for branch, units in inp.sales_yesterday:
        old__quantity(f"sales_yesterday {branch} units", units)
    b = inp.bookings
    for branch, day, units in b.demand:
        old__quantity(f"bookings.demand {branch} day {day} units", units)
    for branch, day, totes in b.totes:
        old__quantity(f"bookings.totes {branch} day {day} totes", totes)
    for run_code, totes in b.run_totes:
        old__quantity(f"bookings.run_totes {run_code} totes", totes)
    for lot_id, units in b.lot_units:
        old__quantity(f"bookings.lot_units {lot_id} units", units)
    seen: set[str] = set()
    for lot_id in (*(l.lot_id for l in inp.lots), *(i.inbound_id for i in inp.inbound)):  # simulate keys both by id
        if lot_id in seen:
            raise InputError(f"duplicate lot_id {lot_id!r} across lots and inbound")
        seen.add(lot_id)
    codes: set[str] = set()
    for r in inp.runs:
        if r.run_code in codes:
            raise InputError(f"duplicate run_code {r.run_code!r}")
        codes.add(r.run_code)
    # D-18 WR-06: the Policy money guards and time fields, product and run costs, the decision minute, the case lot
    pol = inp.policy
    # 02-CONTEXT D-33 (review WR-09): a transfer needs at least one sellable day at the destination, so a run arriving
    # after the case lot's expiry is refused by the sellable-days limit, never reaching an empty window in
    # limits.transfer_caps; review WR-10: the value prints through _shown
    if pol.min_sellable_days < 1:
        raise InputError(f"policy.min_sellable_days={old__shown(pol.min_sellable_days)} must be >= 1")
    for name in ("min_regret", "crossover_range_floor", "adjustment_cost_max", "packing_min", "min_sellable_days",
                 "relabel_min", "markdown_same_day_by_min", "sellable_same_day_before_min",
                 "stale_after_days", "phantom_min_forecast", "baseline_near_expiry_days", "waste_price",
                 "waste_flip_max"):
        old__quantity(f"policy.{name}", getattr(pol, name))
    # round-3 IN-03: the stale factor is a share of the record; the baseline marks down to an allowed step
    if not (math.isfinite(pol.stale_factor) and 0 <= pol.stale_factor <= 1):
        raise InputError(f"policy.stale_factor={pol.stale_factor} must be in 0..1")
    if pol.baseline_markdown_pct not in (20, 30):
        raise InputError(f"policy.baseline_markdown_pct={pol.baseline_markdown_pct} must be 20 or 30")
    # rev 6.3.0 (ENG-09): the shortlist is off (0) or a step of 5 or 10
    if pol.shortlist_step not in (0, 5, 10):
        raise InputError(f"policy.shortlist_step={pol.shortlist_step} must be 0, 5 or 10")
    for name in ("price", "disposal_cost", "kg_per_unit"):
        old__quantity(f"product.{name}", getattr(inp.product, name))
    for r in inp.runs:
        for name in ("fixed_cost", "per_unit_cost"):
            old__quantity(f"run {r.run_code} {name}", getattr(r, name))
        if r.cancel_cost is not None:
            old__quantity(f"run {r.run_code} cancel_cost", r.cancel_cost)
    if not 0 <= inp.decision_min <= 1439:
        raise InputError(f"decision_min={inp.decision_min} outside 0..1439")
    if not any(l.lot_id == inp.case_lot_id for l in inp.lots):
        raise InputError(f"case_lot_id {inp.case_lot_id!r} is not in lots")
    # rev 6.3.0 (ESL-01, D-08): the markdown schedule follows the one shared rule
    reason = schedule_problem(inp.markdown_schedule)
    if reason is not None:
        raise InputError(f"markdown_schedule={inp.markdown_schedule!r}: {reason}")


# ---- END ORACLE


# ---- the type-exact comparison

def canon(x):
    """A type-exact form: dataclasses field by field (applied_overrides included), containers with their type, dicts
    in key order, floats by hex (so -0.0, inf and NaN differ), ints by type and value, Decimals by str, any other
    object by type and identity."""
    t = type(x)
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return (t, tuple((f.name, canon(getattr(x, f.name))) for f in dataclasses.fields(x)))
    if t is float:
        return (t, x.hex())
    if t is int or t is bool or t is str or x is None:
        return (t, x)
    if t is Decimal:
        return (t, str(x))
    if t is tuple or t is list:
        return (t, tuple(canon(v) for v in x))
    if t is dict:
        return (t, tuple((canon(k), canon(v)) for k, v in x.items()))
    return (t, id(x))


def outcome(fn, *args):
    """("ok", the type-exact value) or ("raise", type, message, cause type)."""
    try:
        return ("ok", canon(fn(*args)))
    except Exception as exc:  # noqa: BLE001 - the oracle compares every exception
        return ("raise", type(exc), str(exc), type(exc.__cause__))


# ---- the corpus

def _numify(inp: EngineInput, form: str) -> EngineInput:
    """inp with every declared number rewritten: 'int' (whole floats as ints), 'dec' (Decimals), 'wfloat' (ints as
    floats). The coerced result must equal the old code's either way."""
    def num(v, kind):
        if v is None or type(v) is bool:
            return v
        if form == "dec":
            return Decimal(repr(v)) if type(v) is float else Decimal(v)
        if form == "int" and kind == "f" and type(v) is float and v.is_integer():
            return int(v)
        if form == "wfloat" and kind == "i" and type(v) is int:
            return float(v)
        return v

    def obj(o):
        ch = {n: num(getattr(o, n), "f") for n in old_FLOAT_FIELDS[type(o)]}
        ch.update({n: num(getattr(o, n), "i") for n in old_INT_FIELDS[type(o)]})
        return replace(o, **ch)

    def row(r, types):
        return tuple(num(v, t) if t != "-" else v for t, v in zip(types, r))

    b = inp.bookings
    return replace(
        inp, today=num(inp.today, "i"), decision_min=num(inp.decision_min, "i"), product=obj(inp.product),
        lots=tuple(map(obj, inp.lots)), inbound=tuple(map(obj, inp.inbound)), runs=tuple(map(obj, inp.runs)),
        forecasts=tuple(map(obj, inp.forecasts)), capacity=tuple(map(obj, inp.capacity)), policy=obj(inp.policy),
        sales_yesterday=tuple(row(r, "-f") for r in inp.sales_yesterday),
        markdown=MarkdownResponse(row(inp.markdown.m20, "fff"), row(inp.markdown.m30, "fff")),
        bookings=Bookings(tuple(row(r, "-if") for r in b.demand), tuple(row(r, "-ii") for r in b.totes),
                          tuple(row(r, "-i") for r in b.run_totes), tuple(row(r, "-f") for r in b.lot_units),
                          b.lot_price),
        markdown_schedule=tuple(row(r, "ii") for r in inp.markdown_schedule))


def _running(inp: EngineInput, n: int) -> EngineInput:
    """inp with n running rows per bookings group, shaped as advance.baseline_morning builds them: storage
    (dest, int day, int totes), trucks (run_code, int), set-aside (lot_id, float), priced lot ids, plus shelfshift-style
    demand rows (branch, int day, float)."""
    days = (THU, FRI, SAT)
    return replace(inp, bookings=Bookings(
        demand=tuple((LAT_PHRAO if i % 2 else BANG_NA, days[i % 3], float(i % 7) + 0.25) for i in range(n)),
        totes=tuple((LAT_PHRAO, d, int(q)) for q, d in ((i % 5, days[i % 3]) for i in range(n))),
        run_totes=tuple(("R-02", int(i % 4)) for i in range(n)),
        lot_units=tuple((f"MLK-{i % 50:03d}", float(i % 9)) for i in range(n)),
        lot_price=tuple(f"MLK-{i:03d}" for i in range(n))))


def _rich() -> EngineInput:
    """f6 plus two inbound deliveries, a second run, a markdown schedule, policy and 30 running rows per group."""
    base = f6()
    r03 = replace(base.runs[0], run_code="R-03", day=FRI, cancel_cost=120.0)
    inbound = (Inbound("IN-1", LAT_PHRAO, 24.0, FRI, 360, SAT, 2), Inbound("IN-2", BANG_NA, 48.0, SAT, 420, SAT + 2, 3))
    return _running(replace(base, inbound=inbound, runs=base.runs + (r03,), markdown_schedule=((2, 20), (1, 30)),
                            policy=Policy(waste_price=0.5)), 30)


def _good() -> list[EngineInput]:
    """(i) the engine fixtures and test_adjust.py's variants, as numbers of each form; (ii) running-Bookings inputs."""
    variants = [
        f1(), f6(), _rich(),
        with_facts(f6(), {"kind": "stock_count", "lot_id": "MLK-038", "units": 0}),
        with_facts(f1(), {"kind": "expiry", "lot_id": "MLK-042", "expiry_day": Decimal(FRI)}),
        with_overrides(f1(), {"kind": "markdown_response", "m20": 2.5, "m30": 2}),
        with_overrides(f1(), {"kind": "demand_multiplier", "branch": LAT_PHRAO, "day_from": 4.0, "day_to": 99,
                              "value": Decimal("1.5")}),
        with_overrides(with_facts(f1(), {"kind": "capacity", "branch": LAT_PHRAO, "day_from": FRI, "day_to": 99,
                                         "free_totes": 3}), {"kind": "transport_quote", "run_code": "R-02",
                                                             "fixed_cost": 450}),
        with_facts(f1(), {"kind": "decide_at", "minute": 630}),
        replace(f1(), applied_overrides=["decide_at"]),
    ]
    out = []
    for v in variants:
        out += [v, _numify(v, "int"), _numify(v, "dec"), _numify(v, "wfloat")]
    big = _running(f1(), 640)
    b = big.bookings
    out += [big, _numify(big, "dec"), _numify(big, "int"), _numify(big, "wfloat"),
            replace(big, bookings=Bookings(list(map(list, b.demand)), list(map(list, b.totes)),
                                           list(map(list, b.run_totes)), list(map(list, b.lot_units)),
                                           list(b.lot_price))),
            replace(big, bookings=Bookings(
                tuple((x, d, Decimal(u)) if i % 3 == 0 else (x, d, int(u)) if u.is_integer() else (x, d, u)
                      for i, (x, d, u) in enumerate(b.demand)),
                tuple((x, float(d), Decimal(t)) if i % 2 else (x, d, t) for i, (x, d, t) in enumerate(b.totes)),
                tuple((x, Decimal(t)) if i % 2 else (x, float(t)) for i, (x, t) in enumerate(b.run_totes)),
                tuple((x, int(u)) if i % 2 else (x, Decimal(u)) for i, (x, u) in enumerate(b.lot_units)),
                b.lot_price))]
    return out


class _F(float):
    pass


class _I(int):
    pass


class _NoFormat:
    def __format__(self, spec):
        raise RuntimeError("format refused")

    def __repr__(self):
        raise RuntimeError("repr refused")


class _NoRepr:
    def __repr__(self):
        raise RuntimeError("repr refused")


PALETTE = (True, False, Decimal("2"), Decimal("2.5"), Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"),
           Decimal("1e5000"), math.nan, math.inf, -math.inf, -1, -1.0, -0.0, 0.5, 2.0, 10 ** 400, 10 ** 5000, "1", None,
           _F(2.0), _I(2))
SHAPES = (("a", 1), ("a", 1, 2.0, 3), ["S1", 2, 1], "abc", None, {"a": 1}, ())


def _set_obj(inp, group, idx, field, value):
    seq = list(getattr(inp, group))
    seq[idx] = replace(seq[idx], **{field: value})
    return replace(inp, **{group: tuple(seq)})


def _set_row(rows, idx, pos, value):
    rows = list(rows)
    if pos is None:
        rows[idx] = value
    else:
        r = list(rows[idx])
        r[pos] = value
        rows[idx] = tuple(r)
    return tuple(rows)


def _slots(base: EngineInput):
    """Functions value -> input, one per slot: every declared number (first and last object), the markdown triples,
    the schedule steps, sales_yesterday, and the first, middle and last row of each bookings group at every
    position."""
    out = [lambda v: replace(base, today=v), lambda v: replace(base, decision_min=v)]
    for n in (*old_FLOAT_FIELDS[Product], *old_INT_FIELDS[Product]):
        out.append(lambda v, n=n: replace(base, product=replace(base.product, **{n: v})))
    for n in (*old_FLOAT_FIELDS[Policy], *old_INT_FIELDS[Policy]):
        out.append(lambda v, n=n: replace(base, policy=replace(base.policy, **{n: v})))
    for group, cls in (("lots", Lot), ("inbound", Inbound), ("runs", Run), ("forecasts", Forecast),
                       ("capacity", Capacity)):
        for idx in sorted({0, len(getattr(base, group)) - 1}):
            for n in (*old_FLOAT_FIELDS[cls], *old_INT_FIELDS[cls]):
                out.append(lambda v, g=group, i=idx, n=n: _set_obj(base, g, i, n, v))
    for name in ("m20", "m30"):
        for pos in range(3):
            out.append(lambda v, name=name, pos=pos: replace(base, markdown=replace(
                base.markdown, **{name: _set_row([getattr(base.markdown, name)], 0, pos, v)[0]})))
    for idx in range(len(base.markdown_schedule)):
        for pos in range(2):
            out.append(lambda v, i=idx, p=pos: replace(base, markdown_schedule=_set_row(base.markdown_schedule, i, p, v)))
    for idx in range(len(base.sales_yesterday)):
        for pos in range(2):
            out.append(lambda v, i=idx, p=pos: replace(base, sales_yesterday=_set_row(base.sales_yesterday, i, p, v)))
    b = base.bookings
    for group, width in (("demand", 3), ("totes", 3), ("run_totes", 2), ("lot_units", 2), ("lot_price", None)):
        rows = getattr(b, group)
        for idx in sorted({0, len(rows) // 2, len(rows) - 1}):
            for pos in (range(width) if width else (None,)):
                out.append(lambda v, g=group, i=idx, p=pos: replace(base, bookings=replace(
                    base.bookings, **{g: _set_row(getattr(base.bookings, g), i, p, v)})))
    return out


def _shape_cases(base: EngineInput):
    """Wrong-arity and non-tuple rows, in every row-shaped slot."""
    out = []
    b = base.bookings
    for shape in SHAPES:
        for group in ("demand", "totes", "run_totes", "lot_units"):
            for idx in (0, len(getattr(b, group)) - 1):
                out.append(replace(base, bookings=replace(b, **{group: _set_row(getattr(b, group), idx, None, shape)})))
        out.append(replace(base, sales_yesterday=_set_row(base.sales_yesterday, 0, None, shape)))
        out.append(replace(base, markdown=replace(base.markdown, m20=shape)))
        out.append(replace(base, markdown_schedule=_set_row(base.markdown_schedule, 1, None, shape)))
    out += [replace(base, markdown_schedule=s) for s in (None, "2:20 1:30", {2: 20}, [[2, 20], [1, 30]])]
    return out


def _unprintable_cases(base: EngineInput):
    """Objects whose __format__ or __repr__ raises, in id and branch slots."""
    out = []
    for bad in (_NoFormat(), _NoRepr()):
        out += [_set_obj(base, "lots", 0, "lot_id", bad), _set_obj(base, "lots", 0, "branch", bad),
                _set_obj(base, "inbound", 1, "inbound_id", bad), _set_obj(base, "runs", 1, "run_code", bad),
                _set_obj(base, "forecasts", 0, "branch", bad), _set_obj(base, "capacity", 2, "branch", bad),
                replace(base, sales_yesterday=_set_row(base.sales_yesterday, 0, 0, bad)),
                replace(base, case_lot_id=bad), replace(base, engine_version=bad)]
        for group in ("demand", "totes", "run_totes", "lot_units", "lot_price"):
            pos = None if group == "lot_price" else 0
            out.append(replace(base, bookings=replace(base.bookings, **{
                group: _set_row(getattr(base.bookings, group), 3, pos, bad)})))
    return out


def _corpus():
    rich = _rich()
    cases = _good()
    for slot in _slots(rich):
        cases += [slot(v) for v in PALETTE]
    cases += _shape_cases(rich) + _unprintable_cases(rich)
    # a day too long to print after its coerce, together with a later failing check: the old screen's second coerce
    # raised first
    huge = Decimal("1e5000")
    bad_lot = _set_obj(rich, "lots", 0, "markdown_pct", 10)
    cases += [_set_obj(bad_lot, "forecasts", 0, "day", huge), _set_obj(bad_lot, "capacity", 1, "day", huge),
              _set_obj(_set_obj(rich, "forecasts", 2, "low", -1.0), "capacity", 0, "day", huge),
              _set_obj(_set_obj(rich, "capacity", 0, "day", huge), "forecasts", 1, "day", huge),
              _set_obj(rich, "capacity", 0, "day", huge)]
    return cases


CORPUS = _corpus()


# ---- the tests

def test_oracle_corpus_is_broad():
    assert len(CORPUS) > 2500
    assert outcome(old_validate_input, _rich()) == ("ok", canon(None))  # the palette's base is a valid input
    assert sum(len(x.bookings.totes) >= 600 for x in CORPUS if isinstance(x.bookings.totes, (tuple, list))) >= 6


def test_coerce_input_is_identical_to_the_oracle():
    for x in CORPUS:
        assert outcome(coerce_input, x) == outcome(old_coerce_input, x), x


def test_validate_input_is_identical_to_the_oracle():
    for x in CORPUS:
        assert outcome(validate_input, x) == outcome(old_validate_input, x), x


def test_rows_are_never_accepted_by_value():
    """Within one input and in later calls, an exact (S1, 2, 1) row never lets an equal bool, float or Decimal row
    through the fast path: each takes the old path's result (the bool raises 'must be a whole number, not a bool')."""
    base = _rich()
    rows = (("S1", 2, 1), ("S1", 2, True), ("S1", 2, 1.0), ("S1", 2, Decimal(1)), ("S1", 2, 1))
    one = replace(base, bookings=replace(base.bookings, totes=rows))
    assert outcome(coerce_input, one) == outcome(old_coerce_input, one)
    assert "must be a whole number, not a bool" in outcome(coerce_input, one)[2]
    for r in rows:
        x = replace(base, bookings=replace(base.bookings, totes=(("S1", 2, 1), r)))
        assert outcome(coerce_input, x) == outcome(old_coerce_input, x)
        assert outcome(validate_input, x) == outcome(old_validate_input, x)
    ok = outcome(coerce_input, replace(base, bookings=replace(base.bookings, totes=(("S1", 2, 1.0),))))
    assert ok[0] == "ok" and ok == outcome(old_coerce_input, replace(base, bookings=replace(base.bookings,
                                                                                          totes=(("S1", 2, 1.0),))))
    for v in (True, 1.0, Decimal(1), _I(1)):
        lot = replace(base.lots[0], received_seq=v)
        x = replace(base, lots=(replace(base.lots[0], received_seq=1), lot, *base.lots[1:]))
        assert outcome(coerce_input, x) == outcome(old_coerce_input, x)


def _unprintable(day) -> bool:
    try:
        str(day)
    except ValueError:
        return True
    return False


def test_coerce_input_is_idempotent_on_its_output():
    """coerce_input(coerce_input(x)) is type-exact equal to coerce_input(x), old and new, with one exception that the
    oracle shows: a forecast or capacity day given as a Decimal such as 1e5000 coerces to an int too long to print,
    and the second coerce's label for it then raises ValueError. That is why checked_input repeats exactly those
    labels before the checks, as the second coerce in the old screen did."""
    for coerce in (old_coerce_input, coerce_input):
        refused = 0
        for x in CORPUS:
            try:
                once = coerce(x)
            except Exception:  # noqa: BLE001 - only inputs that coerce are checked here
                continue
            again = outcome(coerce, once)
            if again[0] == "raise":
                assert again[1:] == (ValueError, str(again[2]), type(None)), x
                assert "integer string conversion" in again[2]
                assert any(_unprintable(o.day) for o in (*once.forecasts, *once.capacity)), x
                refused += 1
            else:
                assert again == ("ok", canon(once)), x
        assert refused >= 2  # the corpus holds the exception, in a forecast and in a capacity


def test_canonical_to_dict_is_identical_under_the_oracle(monkeypatch):
    goods = _good()
    new = [engine.canonical.to_dict(x) for x in goods]
    monkeypatch.setattr(engine.canonical, "coerce_input", old_coerce_input)
    old = [engine.canonical.to_dict(x) for x in goods]
    assert [canon(d) for d in new] == [canon(d) for d in old]


def _old_checked(x):
    """The old screen: inp = old_coerce_input(x), then old_validate_input(inp), which coerces again."""
    y = old_coerce_input(x)
    old_validate_input(y)
    return y


def test_checked_input_is_coerce_then_validate_under_the_oracle():
    """checked_input(x) returns old_coerce_input(x); it raises what old_coerce_input(x) raises, or else what
    old_validate_input(old_coerce_input(x)) raises."""
    from engine.adjust import checked_input
    for x in CORPUS:
        assert outcome(checked_input, x) == outcome(_old_checked, x), x


def test_lazy_messages_raise_what_the_eager_ones_did():
    """validate_input builds a row's message only when printing it cannot raise (quick 261017-tps, Task 2 c): a day
    that coerces to an int too long to print, an id whose __format__ raises or that is 10**5000, a forecast or
    capacity day too long to print and an int-subclass value all still raise, or pass, exactly as before."""
    rich = _rich()
    b = rich.bookings
    huge, bad = Decimal("1e5000"), _NoFormat()
    cases = [
        replace(rich, bookings=replace(b, totes=_set_row(b.totes, 0, 1, huge))),
        replace(rich, bookings=replace(b, demand=_set_row(b.demand, 4, 1, huge))),
        replace(rich, bookings=replace(b, demand=_set_row(b.demand, 4, 0, bad))),
        replace(rich, bookings=replace(b, run_totes=_set_row(b.run_totes, 2, 0, 10 ** 5000))),
        replace(rich, bookings=replace(b, lot_units=_set_row(b.lot_units, 2, 0, bad))),
        replace(rich, bookings=replace(b, totes=_set_row(b.totes, 5, 0, 10 ** 5000))),
        _set_obj(rich, "forecasts", 1, "day", huge), _set_obj(rich, "capacity", 2, "day", huge),
        replace(rich, bookings=replace(b, totes=_set_row(b.totes, 1, 2, _I(3)))),
        replace(rich, bookings=replace(b, totes=_set_row(b.totes, 1, 2, 10 ** 76))),
        replace(rich, bookings=replace(b, totes=_set_row(b.totes, 1, 2, 10 ** 77 + 1))),
        replace(rich, bookings=replace(b, demand=_set_row(b.demand, 1, 2, 1.2e77))),
        replace(rich, bookings=replace(b, demand=_set_row(b.demand, 1, 2, 1.1e77))),
    ]
    raised = 0
    for x in cases:
        got = outcome(validate_input, x)
        assert got == outcome(old_validate_input, x), x
        raised += got[0] == "raise"
    assert raised >= 9
