"""Overrides (what-ifs, adopted assumptions) and reported facts (spec §7 Overrides).

Each adjustment is a dict with a "kind" and exactly the fields listed in KINDS, except that a field listed in
OPTIONAL may be left out.
Unknown kinds or keys, wrong types and out-of-bounds values are rejected.
"""
from __future__ import annotations

import math
from dataclasses import replace

from .model import (ENGINE_VERSION, Bookings, Capacity, EngineInput, Forecast, Inbound, Lot, MarkdownResponse, Policy,
                    Product, Run, reduced_schedule, schedule_problem)

MONEY = "MONEY"  # bound marker: 0 to the input's policy.adjustment_cost_max (D-14)
EXPIRY_DAY = "EXPIRY_DAY"  # bound marker: yesterday (a confirmed label may show an expired lot) to the last forecast day
COUNTED_DAY = "COUNTED_DAY"  # bound marker: any day up to today; a count can't come from a later day (D-18 WR-02)
MULT = (0.5, 3.0)
HOURS = (0.0, 24.0)
UNITS = (0.0, 100_000.0)

# kind -> {field: (type, bounds: a (low, high) tuple, a bound marker or None)}
# Day ranges (capacity, demand_multiplier) have no horizon bound (D-16). A range changes the input's days
# max(day_from, today)..day_to, so a range that has ended or lies wholly in the future changes nothing and stays in
# the input and its version. day_from <= day_to is still checked.
KINDS: dict[str, dict[str, tuple[type, tuple[float, float] | str | None]]] = {
    "run_available": {"run_code": (str, None), "value": (bool, None)},
    "cold_chain": {"run_code": (str, None), "value": (bool, None)},
    "decide_at": {"minute": (int, (0, 1439))},
    "departure_delay_h": {"run_code": (str, None), "hours": (float, HOURS)},
    "capacity": {"branch": (str, None), "day_from": (int, None), "day_to": (int, None),
                 "free_totes": (int, (0, 10_000))},
    "transport_quote": {"run_code": (str, None), "fixed_cost": (float, MONEY)},
    "handling_cost": {"run_code": (str, None), "per_unit_cost": (float, MONEY)},
    "markdown_response": {"m20": (float, MULT), "m30": (float, MULT)},
    "demand_multiplier": {"branch": (str, None), "day_from": (int, None), "day_to": (int, None),
                          "value": (float, MULT)},
    "disposal_cost": {"value": (float, MONEY)},
    "stock_count": {"lot_id": (str, None), "units": (float, UNITS), "counted_day": (int, COUNTED_DAY)},
    "expiry": {"lot_id": (str, None), "expiry_day": (int, EXPIRY_DAY)},
}
# kind -> the keys a payload may leave out; stock_count.counted_day defaults to the evaluation day (D-21 choice 1)
OPTIONAL = {"stock_count": {"counted_day"}}
ADOPTABLE = {"transport_quote", "handling_cost", "markdown_response", "demand_multiplier", "disposal_cost"}
WHAT_IF_ONLY = {"run_available", "cold_chain", "decide_at", "departure_delay_h", "capacity"}
FACT_ONLY = {"stock_count", "expiry"}
# kinds a confirmed field signal may set (spec §7 Field signals); decide_at is never a fact
REPORTABLE = {"capacity", "demand_multiplier", "transport_quote", "handling_cost", "run_available", "cold_chain",
              "departure_delay_h", "stock_count", "expiry"}


class AdjustmentError(ValueError):
    pass


class InputError(ValueError):
    pass


MARKDOWN_PCTS = (0, 20, 30)

# Declared number types, field by field (spec §6 EngineInput, "Version"): 500 and 500.0 must hash alike.
FLOAT_FIELDS = {
    Product: ("price", "disposal_cost", "kg_per_unit"),
    Lot: ("qty",),
    Inbound: ("qty",),
    Run: ("fixed_cost", "per_unit_cost", "cancel_cost"),
    Forecast: ("low", "base", "high"),
    Capacity: (),
    Policy: ("min_regret", "crossover_range_floor", "adjustment_cost_max", "stale_factor", "phantom_min_forecast",
             "waste_price", "waste_flip_max"),
}
INT_FIELDS = {
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
NONE_ALLOWED = {(Lot, "last_verified_day"), (Run, "cancel_cost")}


def _is_decimal(value) -> bool:
    """A Decimal-like number (pg8000 returns NUMERIC as Decimal), detected without the decimal module."""
    return not isinstance(value, (bool, int, float, str)) and hasattr(value, "as_integer_ratio")


def _float(name: str, value) -> float:
    """Float-typed field: an int, float or Decimal-like becomes a float. A bool, NaN, a str or None is an InputError."""
    if isinstance(value, bool):
        raise InputError(f"{name}={value!r} must be a number, not a bool")
    if isinstance(value, (int, float)) or _is_decimal(value):
        try:
            out = float(value)
        except (ValueError, OverflowError) as exc:  # e.g. a signalling NaN or a huge int
            raise InputError(f"{name}={value!r} must be a number") from exc
        if math.isnan(out):
            raise InputError(f"{name}={value!r} must not be NaN")
        return out
    raise InputError(f"{name}={value!r} must be a number")


def _int(name: str, value) -> int:
    """Int-typed field: an int is kept; a float or Decimal-like becomes an int only when it is a whole number.

    A bool, a fraction, NaN, infinity, a str or None is an InputError.
    """
    if isinstance(value, bool):
        raise InputError(f"{name}={value!r} must be a whole number, not a bool")
    if isinstance(value, int):
        return value
    if isinstance(value, float) or _is_decimal(value):
        try:
            num, den = value.as_integer_ratio()
        except (ValueError, OverflowError) as exc:  # NaN or infinity
            raise InputError(f"{name}={value!r} must be a whole number") from exc
        if den != 1:
            raise InputError(f"{name}={value!r} must be a whole number")
        return num
    raise InputError(f"{name}={value!r} must be a whole number")


def _typed(obj, label: str):
    """Rebuild one dataclass with its numbers coerced by declared type. Messages read '<label><field>=...'.

    Exact types only, never by value (quick 261017-tps): when every declared number is already an exact non-NaN float
    or an exact int (or None where None is allowed), the rebuilt object would hold the very same values, so obj is
    returned as it is. A bool, Decimal, NaN or subclass takes the full path below."""
    cls = type(obj)
    floats, ints = FLOAT_FIELDS[cls], INT_FIELDS[cls]  # an unknown class is a KeyError, as on the full path
    for name in floats:
        v = getattr(obj, name)
        if not (type(v) is float and v == v or v is None and (cls, name) in NONE_ALLOWED):
            break
    else:
        for name in ints:
            v = getattr(obj, name)
            if not (type(v) is int or v is None and (cls, name) in NONE_ALLOWED):
                break
        else:
            return obj

    def coerce(name: str, convert):
        value = getattr(obj, name)
        if value is None and (cls, name) in NONE_ALLOWED:
            return None
        return convert(f"{label}{name}", value)

    changes = {name: coerce(name, _float) for name in FLOAT_FIELDS[cls]}
    changes.update({name: coerce(name, _int) for name in INT_FIELDS[cls]})
    return replace(obj, **changes)


def _row(row, types: str, label: str) -> tuple:
    """Coerce one tuple row position by position: 'f' float, 'i' int, '-' unchanged.

    A row that is not a tuple or list, or has a different number of values than types, is an InputError naming
    the field, never a bare ValueError or IndexError from unpacking it later (round-3 IN-02).

    Exact types only, never by value (quick 261017-tps): a tuple of the right length whose 'f' values are exact
    non-NaN floats and whose 'i' values are exact ints is what the full path would rebuild from the very same values,
    so it is returned as it is. A list, a bool, a Decimal or a subclass takes the full path."""
    if type(row) is tuple and len(row) == len(types):
        for t, v in zip(types, row):
            if t == "-" or (type(v) is int if t == "i" else type(v) is float and v == v):
                continue
            break
        else:
            return row
    if not isinstance(row, (tuple, list)) or len(row) != len(types):
        raise InputError(f"{label} must have {len(types)} values, got {row!r}")
    convert = {"f": _float, "i": _int, "-": lambda name, v: v}
    return tuple(convert[t](f"{label}[{i}]", v) for i, (t, v) in enumerate(zip(types, row)))


def _payload(adj):
    """Convert an adjustment's numbers by KINDS field types, only where exact. Everything else, and unknown kinds and
    keys, pass through unchanged: validate names them with AdjustmentError."""
    kind = adj.get("kind") if isinstance(adj, dict) else None
    spec = KINDS.get(kind) if isinstance(kind, str) else None
    if spec is None:
        return adj
    out = {}
    for key, value in adj.items():
        typ = spec[key][0] if key in spec else None
        if isinstance(value, bool):
            pass
        elif typ is float and (isinstance(value, (int, float)) or _is_decimal(value)):
            try:
                as_float = float(value)
            except (ValueError, OverflowError):
                as_float = math.nan
            if not math.isnan(as_float):
                value = as_float
        elif typ is int and (isinstance(value, float) or _is_decimal(value)):
            try:
                num, den = value.as_integer_ratio()
            except (ValueError, OverflowError):
                den = 0
            if den == 1:
                value = num
        out[key] = value
    return out


def coerce_input(inp: EngineInput) -> EngineInput:
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
        today=_int("today", inp.today),
        decision_min=_int("decision_min", inp.decision_min),
        product=_typed(inp.product, "product."),
        lots=tuple(_typed(l, f"lot {l.lot_id} ") for l in inp.lots),
        inbound=tuple(_typed(i, f"inbound {i.inbound_id} ") for i in inp.inbound),
        runs=tuple(_typed(r, f"run {r.run_code} ") for r in inp.runs),
        forecasts=tuple(_typed(f, f"forecast {f.branch} day {f.day} ") for f in inp.forecasts),
        capacity=tuple(_typed(c, f"capacity {c.branch} day {c.day} ") for c in inp.capacity),
        sales_yesterday=tuple(_row(r, "-f", f"sales_yesterday {r[0]}" if isinstance(r, (tuple, list)) and r
                                   else "sales_yesterday") for r in inp.sales_yesterday),
        markdown=MarkdownResponse(_row(md.m20, "fff", "markdown.m20"), _row(md.m30, "fff", "markdown.m30")),
        policy=_typed(inp.policy, "policy."),
        bookings=Bookings(tuple(_row(r, "-if", "bookings.demand") for r in b.demand),
                          tuple(_row(r, "-ii", "bookings.totes") for r in b.totes),
                          tuple(_row(r, "-i", "bookings.run_totes") for r in b.run_totes),
                          tuple(_row(r, "-f", "bookings.lot_units") for r in b.lot_units),
                          tuple(b.lot_price)),
        reported_facts=tuple(_payload(a) for a in inp.reported_facts),
        overrides=tuple(_payload(a) for a in inp.overrides),
        applied_overrides=tuple(inp.applied_overrides),
        markdown_schedule=tuple(_row(r, "ii", f"markdown_schedule[{i}]") for i, r in enumerate(schedule)),
    )


def _fits(value) -> bool:
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


def _shown(value, too_large: str = "a whole number too large to print") -> str:
    """The value as given, or a fixed phrase for an int Python refuses to print (review WR-10, IN-12).

    CPython refuses int-to-str past sys.get_int_max_str_digits() digits: 4,300 by default, as low as 640 under
    -X int_max_str_digits or PYTHONINTMAXSTRDIGITS. Catching that refusal, never predicting the limit, keeps a message
    about a huge int an InputError naming its field.
    """
    try:
        return repr(value)
    except ValueError:  # an int past sys.get_int_max_str_digits()
        return too_large


# quick 261017-tps: _fine(v) is a sufficient condition for _fits(v) on an exact number, and an int inside +-_PRINTS has
# fewer than 640 digits, so it always prints (CPython's int-to-str limit is never below 640 digits)
_SMALL_INT = 10 ** 76
_PRINTS = 10 ** 600


def _fine(v) -> bool:
    """True only when v is an exact float or int that _fits: an exact float by _fits' own test (float(v) is v), an
    exact int from 0 to 10**76 (its fourth power is at most 1e304). Anything else, fitting or not, returns False and
    takes _quantity, so the check and its message are exactly as before. Pure: it never raises."""
    return (type(v) is float and v >= 0 and math.isfinite(v * v * v * v)) or (type(v) is int and 0 <= v <= _SMALL_INT)


def _day_prints(day) -> bool:
    """An exact int that always prints: a label holding it cannot raise."""
    return type(day) is int and -_PRINTS < day < _PRINTS


def _quantity(name: str, value: float) -> None:
    if not _fits(value):
        # the value as given; an int Python refuses to print reads 'a whole number too large for a float', which is
        # always true of it (review IN-12: catch the refusal, never predict the limit)
        shown = _shown(value, too_large="a whole number too large for a float")
        # review WR-08 (D-30): the float-range rule; review IN-09 (D-34): the message names no review and prints the
        # value as given
        raise InputError(f"{name}={shown} must be finite, >= 0 and at most about 1.158e77 (the engine's float range)")


def checked_input(inp: EngineInput) -> EngineInput:
    """coerce_input, then validate_input's checks on the coerced input, with one coerce (quick 261017-tps): it returns
    what `inp = coerce_input(inp)` returned and raises what that and a following `validate_input(inp)` raised.

    validate_input coerces its input again. On coerce_input's own output that second coerce changes nothing and fails
    only where a label prints a number the first coerce changed: a forecast or capacity day given as a Decimal such
    as 1e5000 becomes an int too long to print, and its label raises ValueError, before any check. Those labels are
    printed here in the same order, so that still happens first; then the checks run once.
    """
    inp = coerce_input(inp)
    for f in inp.forecasts:
        f"forecast {f.branch} day {f.day} "  # noqa: B018 - printed only for the ValueError above
    for c in inp.capacity:
        f"capacity {c.branch} day {c.day} "  # noqa: B018 - printed only for the ValueError above
    _check_coerced(inp)
    return inp


def validate_input(inp: EngineInput) -> None:
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

    The checks are _check_coerced, shared with checked_input, which a caller that keeps the coerced input uses
    instead of coerce_input then validate_input (quick 261017-tps).
    """
    _check_coerced(coerce_input(inp))


def _check_coerced(inp: EngineInput) -> None:
    """validate_input's checks, on an input coerce_input returned (quick 261017-tps: moved here).

    In the loops over rows a message is built only when a check can fail (quick 261017-tps): when the value is
    _fine and every value its label prints is an exact str or a _day_prints int, the old code built a label that
    could not raise and a _quantity that passed, so both are skipped. Otherwise _quantity runs exactly as before, its
    label built first, so an unprintable label still raises as it did and the order of the checks is unchanged."""
    if inp.engine_version != ENGINE_VERSION:
        raise InputError(f"engine_version {inp.engine_version!r} is not this engine's {ENGINE_VERSION!r}")
    for name in ("m20", "m30"):
        low, base, high = values = getattr(inp.markdown, name)
        if not all(MULT[0] <= v <= MULT[1] for v in values):
            raise InputError(f"markdown.{name}={values} outside {MULT}")
        if not low <= base <= high:
            raise InputError(f"markdown.{name}={values} must have low <= base <= high")
    if inp.product.units_per_tote < 1:
        raise InputError(f"product.units_per_tote={inp.product.units_per_tote} must be >= 1")
    _quantity("product.units_per_tote", inp.product.units_per_tote)  # review WR-08: the float range too
    for l in inp.lots:
        if l.markdown_pct not in MARKDOWN_PCTS:
            raise InputError(f"lot {l.lot_id} markdown_pct={l.markdown_pct} not in {MARKDOWN_PCTS}")
        if not (_fine(l.qty) and type(l.lot_id) is str):
            _quantity(f"lot {l.lot_id} qty", l.qty)
    for i in inp.inbound:
        if not (_fine(i.qty) and type(i.inbound_id) is str):
            _quantity(f"inbound {i.inbound_id} qty", i.qty)
    for f in inp.forecasts:
        printable = type(f.branch) is str and _day_prints(f.day)
        for scenario in ("low", "base", "high"):
            if not (printable and _fine(getattr(f, scenario))):
                _quantity(f"forecast {f.branch} day {f.day} {scenario}", getattr(f, scenario))
    for c in inp.capacity:
        if not (_fine(c.free_totes) and type(c.branch) is str and _day_prints(c.day)):
            _quantity(f"capacity {c.branch} day {c.day} free_totes", c.free_totes)
    for r in inp.runs:
        if not (_fine(r.spare_totes) and type(r.run_code) is str):
            _quantity(f"run {r.run_code} spare_totes", r.spare_totes)
    for branch, units in inp.sales_yesterday:
        if not (_fine(units) and type(branch) is str):
            _quantity(f"sales_yesterday {branch} units", units)
    b = inp.bookings
    for branch, day, units in b.demand:
        if not (_fine(units) and type(branch) is str and _day_prints(day)):
            _quantity(f"bookings.demand {branch} day {day} units", units)
    for branch, day, totes in b.totes:
        if not (_fine(totes) and type(branch) is str and _day_prints(day)):
            _quantity(f"bookings.totes {branch} day {day} totes", totes)
    for run_code, totes in b.run_totes:
        if not (_fine(totes) and type(run_code) is str):
            _quantity(f"bookings.run_totes {run_code} totes", totes)
    for lot_id, units in b.lot_units:
        if not (_fine(units) and type(lot_id) is str):
            _quantity(f"bookings.lot_units {lot_id} units", units)
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
        raise InputError(f"policy.min_sellable_days={_shown(pol.min_sellable_days)} must be >= 1")
    for name in ("min_regret", "crossover_range_floor", "adjustment_cost_max", "packing_min", "min_sellable_days",
                 "relabel_min", "markdown_same_day_by_min", "sellable_same_day_before_min",
                 "stale_after_days", "phantom_min_forecast", "baseline_near_expiry_days", "waste_price",
                 "waste_flip_max"):
        _quantity(f"policy.{name}", getattr(pol, name))
    # round-3 IN-03: the stale factor is a share of the record; the baseline marks down to an allowed step
    if not (math.isfinite(pol.stale_factor) and 0 <= pol.stale_factor <= 1):
        raise InputError(f"policy.stale_factor={pol.stale_factor} must be in 0..1")
    if pol.baseline_markdown_pct not in (20, 30):
        raise InputError(f"policy.baseline_markdown_pct={pol.baseline_markdown_pct} must be 20 or 30")
    # rev 6.3.0 (ENG-09): the shortlist is off (0) or a step of 5 or 10
    if pol.shortlist_step not in (0, 5, 10):
        raise InputError(f"policy.shortlist_step={pol.shortlist_step} must be 0, 5 or 10")
    for name in ("price", "disposal_cost", "kg_per_unit"):
        _quantity(f"product.{name}", getattr(inp.product, name))
    for r in inp.runs:
        for name in ("fixed_cost", "per_unit_cost"):
            _quantity(f"run {r.run_code} {name}", getattr(r, name))
        if r.cancel_cost is not None:
            _quantity(f"run {r.run_code} cancel_cost", r.cancel_cost)
    if not 0 <= inp.decision_min <= 1439:
        raise InputError(f"decision_min={inp.decision_min} outside 0..1439")
    if not any(l.lot_id == inp.case_lot_id for l in inp.lots):
        raise InputError(f"case_lot_id {inp.case_lot_id!r} is not in lots")
    # rev 6.3.0 (ESL-01, D-08): the markdown schedule follows the one shared rule
    reason = schedule_problem(inp.markdown_schedule)
    if reason is not None:
        raise InputError(f"markdown_schedule={inp.markdown_schedule!r}: {reason}")


def _bounds(bound, inp: EngineInput):
    """Resolve a KINDS bound against the input: MONEY is 0 to policy.adjustment_cost_max; EXPIRY_DAY is
    yesterday to the last forecast day; COUNTED_DAY is any day up to today; a tuple or None is unchanged."""
    if bound == MONEY:
        return (0.0, inp.policy.adjustment_cost_max)
    if bound == COUNTED_DAY:
        return (-math.inf, inp.today)
    if bound == EXPIRY_DAY:
        last = max((f.day for f in inp.forecasts), default=inp.today)
        return (inp.today - 1, last)
    return bound


def validate(adj: dict, inp: EngineInput) -> None:
    kind = adj.get("kind")
    if kind not in KINDS:
        raise AdjustmentError(f"unknown kind: {kind!r}")
    spec = KINDS[kind]
    extra = set(adj) - set(spec) - {"kind"}
    missing = set(spec) - set(adj) - OPTIONAL.get(kind, set())
    if extra or missing:
        raise AdjustmentError(f"{kind}: unknown keys {sorted(extra)}, missing keys {sorted(missing)}")
    for name, (typ, bound) in spec.items():
        if name not in adj:  # an OPTIONAL key left out
            continue
        bounds = _bounds(bound, inp)
        value = adj[name]
        if typ is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if typ is int and isinstance(value, bool) or not isinstance(value, typ):
            raise AdjustmentError(f"{kind}.{name} must be {typ.__name__}")
        if bounds and not (bounds[0] <= value <= bounds[1]):
            raise AdjustmentError(f"{kind}.{name}={value} outside {bounds}")
    if "run_code" in adj and adj["run_code"] not in {r.run_code for r in inp.runs}:
        raise AdjustmentError(f"unknown run {adj['run_code']}")
    if "branch" in adj and adj["branch"] not in {f.branch for f in inp.forecasts}:
        raise AdjustmentError(f"unknown branch {adj['branch']}")
    if "lot_id" in adj and adj["lot_id"] not in {l.lot_id for l in inp.lots}:
        raise AdjustmentError(f"unknown lot {adj['lot_id']}")
    if "day_from" in adj and adj["day_from"] > adj["day_to"]:
        raise AdjustmentError("day_from after day_to")


def _apply(inp: EngineInput, adj: dict) -> EngineInput:
    kind = adj["kind"]

    def runs(**changes_for) -> EngineInput:
        return replace(inp, runs=tuple(
            replace(r, **changes_for) if r.run_code == adj["run_code"] else r for r in inp.runs))

    if kind == "run_available":
        return runs(available=adj["value"])
    if kind == "cold_chain":
        return runs(cold_chain=adj["value"])
    if kind == "decide_at":
        return replace(inp, decision_min=adj["minute"])
    if kind == "departure_delay_h":  # the loading cutoff stays: a late truck gives packers no extra time
        delay = round(float(adj["hours"]) * 60)
        return replace(inp, runs=tuple(
            replace(r, departs_min=r.departs_min + delay, arrives_min=r.arrives_min + delay)
            if r.run_code == adj["run_code"] else r for r in inp.runs))
    if kind == "capacity":
        lo, hi, b = max(adj["day_from"], inp.today), adj["day_to"], adj["branch"]
        return replace(inp, capacity=tuple(
            replace(c, free_totes=adj["free_totes"]) if c.branch == b and lo <= c.day <= hi else c
            for c in inp.capacity))
    if kind == "transport_quote":
        return runs(fixed_cost=float(adj["fixed_cost"]))
    if kind == "handling_cost":
        return runs(per_unit_cost=float(adj["per_unit_cost"]))
    if kind == "markdown_response":
        # widen low/high to include the adopted value: low <= base <= high holds on every effective input, so the
        # crossover grid (low..high) brackets the value in force (spec §6 Crossover, rev 6.2.1)
        def widened(low_base_high: tuple[float, float, float], v: float) -> tuple[float, float, float]:
            return (min(low_base_high[0], v), v, max(low_base_high[2], v))

        md = inp.markdown
        return replace(inp, markdown=replace(
            md, m20=widened(md.m20, float(adj["m20"])), m30=widened(md.m30, float(adj["m30"]))))
    if kind == "demand_multiplier":
        lo, hi, b, v = max(adj["day_from"], inp.today), adj["day_to"], adj["branch"], float(adj["value"])
        forecasts = tuple(
            replace(f, low=f.low * v, base=f.base * v, high=f.high * v)
            if f.branch == b and lo <= f.day <= hi else f for f in inp.forecasts)
        # review WR-08: one multiplier is bounded at 3.0, but overrides stack; a stack that takes a forecast past the
        # float-range rule (_fits) is refused, so effective() of a valid input stays inside it
        for f in forecasts:
            if f.branch == b and lo <= f.day <= hi:
                for scenario in ("low", "base", "high"):
                    if not _fits(getattr(f, scenario)):
                        # review WR-08 (D-30): the float-range rule; review IN-09 (D-34): no review ID in the message
                        raise AdjustmentError(
                            f"demand_multiplier on {b} day {f.day}: the stacked multipliers take the {scenario} "
                            f"forecast to {getattr(f, scenario)!r}, past the engine's float range (at most about "
                            "1.158e77)")
        return replace(inp, forecasts=forecasts)
    if kind == "disposal_cost":
        return replace(inp, product=replace(inp.product, disposal_cost=float(adj["value"])))
    if kind == "stock_count":
        # a count from an earlier day is already in the record as a stock correction (spec §7 step 5, §8 Corrected
        # records), so only its day is applied; a count at this snapshot also sets the quantity (D-21 choice 1).
        # A count never makes the verification older: the lot keeps the later of its day and the count's, so two
        # counts give the same lot in either order (round-3 WR-01)
        day = adj.get("counted_day", inp.today)
        changes = {"qty": float(adj["units"])} if day == inp.today else {}
        return replace(inp, lots=tuple(
            replace(l, last_verified_day=day if l.last_verified_day is None else max(day, l.last_verified_day),
                    **changes)
            if l.lot_id == adj["lot_id"] else l for l in inp.lots))
    if kind == "expiry":
        return replace(inp, lots=tuple(
            replace(l, expiry_day=adj["expiry_day"], expiry_source="PRINTED") if l.lot_id == adj["lot_id"] else l
            for l in inp.lots))
    raise AdjustmentError(kind)


def effective(inp: EngineInput) -> EngineInput:
    """Apply reported facts, then overrides, then reduce the markdown schedule. The result has no adjustments left.

    The schedule is reduced once, here, to its deepest-reaching step per percent (model.reduced_schedule, D-26):
    validate_input has already checked it as given, and evaluate, check_plan, fact_checks and baseline_plan all
    compute on this effective input, so a schedule's cost never depends on how many redundant steps it has. The
    effective input is what effective_version hashes, so a schedule and its reduced form share it; rec_version of
    the input as entered still differs (canonical.normalized only sorts the schedule).

    The result's applied_overrides is the input's plus the kinds of the overrides applied here, in order (never a
    reported fact's kind), so check_plan can refuse an input on which a what-if was already applied (D-18 WR-01).
    """
    for adj in inp.reported_facts:
        if adj.get("kind") not in REPORTABLE:
            raise AdjustmentError(f"{adj.get('kind')} cannot be a reported fact")
    for adj in inp.overrides:
        if adj.get("kind") in FACT_ONLY:
            raise AdjustmentError(f"{adj['kind']} can only come from a confirmed reported fact")
    out = inp
    for adj in (*inp.reported_facts, *inp.overrides):
        validate(adj, out)
        out = _apply(out, adj)
    return replace(out, reported_facts=(), overrides=(), markdown_schedule=reduced_schedule(out.markdown_schedule),
                   applied_overrides=inp.applied_overrides + tuple(adj["kind"] for adj in inp.overrides))
