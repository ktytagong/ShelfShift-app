"""The public dataset's reference values: FreshRetailNet-50K identity, checksums and calendar (DATA-08, DATA-10), and
the public set's assumptions, codes, labels and rules (DATA-09, DATA-14, DATA-16, DATA-17; 05.2-07).

`reference/public.json` holds every value that describes the public dataset, and every rule the public set adds, as a
`{value, source, type}` leaf, checked by `data.config.leaves`: the creator and license, the two files' sha256, byte
size, row count and date range, the projected columns, the weekday-preserving calendar shift (05.2-CONTEXT D-13), the
stockout rule (D-14), the unknown-price rule (D-17, D-35), the units factor (D-33), the display codes (D-02), the mark
(D-04), the warm-up price response (D-36), the stock rules (D-39), the case-opening screen, the evaluation, board,
catalogue, series and forecast settings (D-11, D-19, D-25, D-26, D-38) and the shortlist step (D-34).
`reference/categories.json` holds the category level and the default row with its overrides (D-20, D-33);
`reference/geography.json` the assumed location and route mapping (D-29..D-31, D-34). The engine Policy and the
markdown response come from `reference/policy.json`, the synthetic set's retailer-policy assumptions, with only
`policy.shortlist_step` replaced. No retailer or dataset value is a code literal here: the loader only checks that the
values agree with each other and that every assumed value is typed Assumption (or Policy), never Data (D-03).

`load()` returns the blocks of public.json by name (`cfg["calendar"]["shift_days"]`) plus `categories`, `geography`,
`policy_json` (the values of reference/policy.json) and `profile_check` (whether category overrides and unmapped stores
were checked against the committed profile, data/reference/reports/public-profile.json, or why not).
`load_for(fixture_json)` lays a fixture's override file over the reference, for tests and the fixture build: its
`dataset`, `calendar` and `units` blocks over public.json, its `geography` block over geography.json (each only at paths
the reference already has), and a `designed` block (the designed example, D-18; the reference sets none, D-40). The
profile describes the full dataset, so a fixture load skips that check and says so.

`fingerprint()` ("ref-" + 12 hex) is taken over public.json, categories.json, geography.json and policy.json, each as
its name and its canonical JSON (as `data.config.fingerprint` does for the synthetic set), so the manifest of a built
set says which reference it was built from (05.2-10).

Every distinct category row (the default, and the default overlaid by each override) goes through the engine's own
input contract (`data.config.self_check`), so a value the engine would refuse is refused here naming the row. A row
whose price is null has no usable base price: `product()` refuses it and `money_available()` is False, so its financial
comparisons show as unavailable and full price never stands in for a missing amount (D-35); its stock still simulates.
"""
from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import hashlib
import json
import pathlib
import re
import sys
from datetime import date, timedelta

from engine.adjust import InputError
from engine.model import Product

from . import config
from .config import ConfigError
from .runs import NetworkParams
from .stock import StockParams

REF_IN = pathlib.Path(__file__).resolve().parent.parent / "reference"
PROFILE = pathlib.Path(__file__).resolve().parent / "reference" / "reports" / "public-profile.json"
SPLITS = ("train", "eval")
FILE_FIELDS = ("name", "sha256", "bytes", "rows", "first_dt", "last_dt")
CODE_KINDS = ("city", "store", "product", "category")
STOCK_FIELDS = ("multiplier", "lookback_days", "same_weekday_obs", "error_share", "error_range", "error_from_day",
                "expiry_estimated_share", "topup_on_eval_days", "seed")
REQUIRED = (
    ("dataset.name", "dataset.creator", "dataset.license", "dataset.license_url", "dataset.source_url",
     "dataset.citation", "dataset.columns")
    + tuple(f"dataset.files.{split}.{field}" for split in SPLITS for field in FILE_FIELDS)
    + ("calendar.shift_days", "calendar.day0", "calendar.first_day", "calendar.last_day", "calendar.demo_open_day",
       "calendar.last_demo_day", "calendar.data_as_of", "calendar.open_min", "calendar.delivery_min",
       "calendar.backtest_days",
       "stockout.column", "stockout.threshold", "stockout.hours_counted", "stockout.long_hours",
       "unknown_price.below_or_equal", "unknown_price.above", "unknown_price.rule", "discount.full_price",
       "units.factor", "units.rule", "codes.note", "mark.text", "truth.price_response.m20",
       "truth.price_response.m30", "case_opening.days_before_first_step", "case_opening.min_waste_units",
       "case_opening.min_discounted_totes", "case_opening.min_short_totes", "evaluation.on_demand",
       "board.default_scope", "board.page_size", "catalogue.kinds", "catalogue.budget", "catalogue.order",
       "catalogue.required", "catalogue.start_kind", "catalogue.warmup_scope", "series.max_days",
       "series.statement_timeout_ms", "policy.shortlist_step", "policy.source", "forecast.model",
       "forecast.fallback", "forecast.confidence_level", "forecast.location", "forecast.dataset", "forecast.runs",
       "budget.max_bytes_billed")
    + tuple(f"codes.{kind}" for kind in CODE_KINDS)
    + tuple(f"stock.{field}" for field in STOCK_FIELDS))
OVERRIDABLE = ("dataset", "calendar", "units", "geography", "designed")  # the blocks a fixture override file may set
OVERRIDE_NEW = ("designed",)  # blocks a fixture may add although the reference has none (the designed example, D-40)
FINGERPRINTED = ("public", "categories", "geography", "policy")  # the reference files a public set is built from
# Blocks of a fingerprinted file that are not inputs of the set: the per-query byte cap guards the cost of the user's
# BigQuery run (user 2026-10-17) and never reaches a part or the manifest, so changing it needs no rebuild.
NOT_FINGERPRINTED = {"public": ("budget",)}
OVERRIDE_IGNORED = ("label",)  # the override file's own description, never merged
SHA256 = re.compile(r"[0-9a-f]{64}")
RAW_ID = re.compile(r"[0-9]+")  # the dataset's ids are integers, kept as their text (data.public)
# The category row: the engine Product fields after sku (data.config.PRODUCT_FIELDS), then the stock fields.
ROW_FIELDS = config.PRODUCT_FIELDS + ("shelf_life_days", "schedule", "order_case", "delivery_cycle_days")
# D-03: these values are assumed, never public facts. Each must be typed Assumption (or Policy where the engine policy
# says so), never Data. Dotted cfg paths; `*` matches any rest of the path.
ASSUMPTION_PATHS = ("units.*", "stock.*", "truth.price_response.*", "categories.default.*",
                    "categories.overrides.*", "geography.*")
ASSUMED_TYPES = ("Assumption", "Policy")
GEOGRAPHY_REQUIRED = ("mapping.note", "positions.rule", "positions.city_radius_km", "positions.seed",
                      "coverage.unmapped", "travel.source", "travel.speed_kmh", "travel.handling_min",
                      "service.same_city", "service.max_travel_min", "service.filters", "runs.k", "runs.days",
                      "runs.loading_cutoff_min", "runs.departs_min", "runs.spare_totes", "runs.cold_chain_share",
                      "costs.fixed_base", "costs.per_km", "costs.per_unit", "costs.cancel")
# Every geography leaf is an Assumption except the service limit and k, which are settings of the search (D-29, D-34).
GEOGRAPHY_POLICY = ("geography.service.max_travel_min", "geography.runs.k")
# The travel-time sources data/runs.py implements: travel_min over the assumed positions (D-29, D-30).
TRAVEL_SOURCES = ("assumed_positions",)
FIXTURE_NO_PROFILE = ("a fixture load: the committed profile describes the full public dataset, not the fixture, so "
                      "category overrides and unmapped stores are not checked against it")


# --------------------------------------------------------------------------------------------------- loading

def load(ref_in: pathlib.Path | str = REF_IN, profile: pathlib.Path | str | None = PROFILE) -> dict:
    """The public reference {block: values} after checking every leaf, the required paths, the calendar, the codes,
    the category rows (each through the engine's self-check) and the geography."""
    ref_in = pathlib.Path(ref_in)
    return _finish(config.read(ref_in / "public.json"), ref_in, _profile(profile))


def load_for(fixture_json: pathlib.Path | str, ref_in: pathlib.Path | str = REF_IN) -> dict:
    """`load()` with a fixture's override file laid over the reference: each leaf the override names replaces the
    reference leaf at the same dotted path. Only the OVERRIDABLE blocks may be set, and only at paths the reference
    already has (`geography` at the paths of geography.json), so a typo is refused instead of ignored; a block in
    OVERRIDE_NEW may be added whole. Every leaf goes through the same checks as the reference's own."""
    fixture_json = pathlib.Path(fixture_json)
    ref_in = pathlib.Path(ref_in)
    tree = config.read(ref_in / "public.json")
    override = config.read(fixture_json)
    unknown = sorted(set(override) - set(OVERRIDABLE) - set(OVERRIDE_IGNORED))
    if unknown:
        raise ConfigError(f"{fixture_json.name}: only {', '.join(OVERRIDABLE)} may be overridden, not "
                          f"{', '.join(unknown)}")
    for block in OVERRIDABLE:
        if block not in override or block == "geography":
            continue
        if block in OVERRIDE_NEW and block not in tree:
            tree[block] = override[block]
        else:
            _merge(tree, {block: override[block]}, fixture_json.name, "")
    return _finish(tree, ref_in, (None, {"checked": False, "reason": FIXTURE_NO_PROFILE}),
                   geography=(override.get("geography"), fixture_json.name))


def _merge(base: dict, over: dict, source: str, prefix: str) -> None:
    for key, sub in over.items():
        where = f"{prefix}.{key}" if prefix else key
        if key not in base:
            raise ConfigError(f"{source}: {where} is not a reference path")
        if isinstance(sub, dict) and "value" not in sub and isinstance(base[key], dict) and "value" not in base[key]:
            _merge(base[key], sub, source, where)
        else:
            base[key] = sub


def _profile(path) -> tuple[dict | None, dict]:
    """(codes the committed profile saw, the check record). Codes: {"stores": set, "categories": {level: set|None}};
    a level's set is None when the profile's codes there are not exactly 0..max (then it cannot list them)."""
    if path is None:
        return None, {"checked": False, "reason": "no profile given"}
    path = pathlib.Path(path)
    if not path.exists():
        return None, {"checked": False, "reason": f"{path.name} is not present"}
    report = json.loads(path.read_text(encoding="utf-8"))
    codes = report["codes"]
    stores = {store for members in codes["stores_by_city"].values() for store in members}
    categories = {}
    for level, stats in report["categories"].items():
        top = codes["max_id"].get(level)
        dense = isinstance(top, int) and stats.get("codes") == top + 1
        categories[level] = {str(i) for i in range(top + 1)} if dense else None
    return {"stores": stores, "categories": categories}, {"checked": True, "against": path.name}


def _companions(ref_in: pathlib.Path, geography: tuple[dict | None, str] = (None, "")
                ) -> tuple[dict, list[tuple[str, dict]]]:
    """({"categories": values, "geography": values or absent}, typed leaves) of the files beside public.json. The
    overrides of categories.json may be empty, so they are checked row by row, each under its own dotted path. A
    fixture's geography override (`(tree, file name)`) is laid over geography.json before any check."""
    out, typed = {}, []
    tree = config.read(ref_in / "categories.json")
    if not isinstance(tree, dict):
        raise ConfigError("categories: must be an object with level, default and overrides")
    overrides = tree.pop("overrides", None)
    if not isinstance(overrides, dict):
        raise ConfigError("categories.overrides: must be an object keyed by category code (it may be empty)")
    typed += config.leaves(tree, "categories")
    values = config.values(tree)
    values["overrides"] = {}
    for code, row in overrides.items():
        typed += config.leaves(row, f"categories.overrides.{code}")
        values["overrides"][code] = config.values(row)
    out["categories"] = values
    geo = ref_in / "geography.json"
    if not geo.exists():
        raise ConfigError("geography: reference/geography.json, the assumed location and route mapping, is required "
                          "(D-30)")
    tree = config.read(geo)
    if geography[0] is not None:
        _merge(tree, geography[0], geography[1], "geography")
    typed += config.leaves(tree, "geography")
    out["geography"] = config.values(tree)
    return out, typed


def _finish(tree: dict, ref_in: pathlib.Path, profile: tuple[dict | None, dict],
            geography: tuple[dict | None, str] = (None, "")) -> dict:
    typed = config.leaves(tree)
    cfg = config.values(tree)
    for path in REQUIRED:
        node = cfg
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                raise ConfigError(f"{path}: required reference value is missing")
            node = node[key]
    _cross_check(cfg)
    _check_codes(cfg)
    companions, more = _companions(ref_in, geography)
    cfg.update(companions)
    _check_assumed(typed + more)
    policy_tree = config.read(ref_in / "policy.json")
    config.leaves(policy_tree, "policy")
    cfg["policy_json"] = config.values(policy_tree)
    settings_block = cfg["policy_json"].get("settings")
    currency = settings_block.get("currency") if isinstance(settings_block, dict) else None
    if not isinstance(currency, str) or not currency.strip():
        raise ConfigError("policy.settings.currency: the currency setting is required (DATA-07)")
    codes, check = profile
    _check_categories(cfg, codes)
    _check_geography(cfg, [(path, node) for path, node in more if path.startswith("geography.")], codes)
    cfg["profile_check"] = check
    return cfg


def _check_assumed(typed: list[tuple[str, dict]]) -> None:
    for path, node in typed:
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in ASSUMPTION_PATHS) and \
                node["type"] not in ASSUMED_TYPES:
            raise ConfigError(f"{path}: an assumed value must be typed Assumption (or Policy), not {node['type']!r}; "
                              "it is never a public-dataset fact (D-03)")


def _date(cfg: dict, path: str) -> date:
    node = cfg
    for key in path.split("."):
        node = node[key]
    try:
        return date.fromisoformat(node)
    except (TypeError, ValueError):
        raise ConfigError(f"{path}: {node!r} is not an ISO date") from None


def _int(cfg: dict, path: str) -> int:
    node = cfg
    for key in path.split("."):
        node = node[key]
    if not isinstance(node, int) or isinstance(node, bool):
        raise ConfigError(f"{path}: {node!r} is not a whole number")
    return node


def _cross_check(cfg: dict) -> None:
    """The calendar agrees with the files (DATA-10, D-13): the shift keeps every weekday; the first train date is
    day0 + first_day, the last train date day0 - 1, the first eval date day0 and the last eval date day0 + last_day;
    Data as of is day0 + demo_open_day. Each failure names the dotted path to fix."""
    shift = timedelta(days=_int(cfg, "calendar.shift_days"))
    day0 = _date(cfg, "calendar.day0")
    first_day, last_day = _int(cfg, "calendar.first_day"), _int(cfg, "calendar.last_day")
    open_day, last_demo = _int(cfg, "calendar.demo_open_day"), _int(cfg, "calendar.last_demo_day")
    for split in SPLITS:
        base = f"dataset.files.{split}"
        if not isinstance(cfg["dataset"]["files"][split]["name"], str) or not cfg["dataset"]["files"][split]["name"]:
            raise ConfigError(f"{base}.name: a file name is required")
        if not isinstance(cfg["dataset"]["files"][split]["sha256"], str) or \
                not SHA256.fullmatch(cfg["dataset"]["files"][split]["sha256"]):
            raise ConfigError(f"{base}.sha256: must be 64 lower-case hex digits")
        for field in ("bytes", "rows"):
            if _int(cfg, f"{base}.{field}") <= 0:
                raise ConfigError(f"{base}.{field}: must be positive")
        for field in ("first_dt", "last_dt"):
            source = _date(cfg, f"{base}.{field}")
            if (source + shift).weekday() != source.weekday():
                raise ConfigError(f"calendar.shift_days: {shift.days} days moves {base}.{field} {source} from a "
                                  f"{source:%A} to a {source + shift:%A}; the shift must keep every weekday (D-13)")
        if _date(cfg, f"{base}.first_dt") > _date(cfg, f"{base}.last_dt"):
            raise ConfigError(f"{base}.first_dt: after {base}.last_dt")
    expected = (("dataset.files.train.first_dt", first_day, "calendar.first_day"),
                ("dataset.files.train.last_dt", -1, "calendar.day0"),
                ("dataset.files.eval.first_dt", 0, "calendar.day0"),
                ("dataset.files.eval.last_dt", last_day, "calendar.last_day"))
    for path, day, names in expected:
        shifted = _date(cfg, path) + shift
        if shifted != day0 + timedelta(days=day):
            raise ConfigError(f"{names}: {path} shifted by calendar.shift_days is {shifted}, but day {day} is "
                              f"{day0 + timedelta(days=day)}")
    if not 0 <= open_day <= last_demo <= last_day:
        raise ConfigError(f"calendar.demo_open_day: need 0 <= demo_open_day ({open_day}) <= last_demo_day "
                          f"({last_demo}) <= last_day ({last_day})")
    as_of = _date(cfg, "calendar.data_as_of")
    if as_of != day0 + timedelta(days=open_day):
        raise ConfigError(f"calendar.data_as_of: {as_of} is not day0 + demo_open_day "
                          f"({day0 + timedelta(days=open_day)})")
    columns = cfg["dataset"]["columns"]
    if not isinstance(columns, list) or not columns or len(set(columns)) != len(columns) or \
            not all(isinstance(c, str) and c for c in columns):
        raise ConfigError("dataset.columns: must be a list of distinct column names")
    if cfg["stockout"]["column"] not in columns:
        raise ConfigError(f"stockout.column: {cfg['stockout']['column']!r} is not in dataset.columns")


# --------------------------------------------------------------------------------------------------- codes (D-02)

def _split(fmt, kind: str) -> tuple[str, str]:
    """(prefix, suffix) of a code format: exactly one `{}`, no other brace, a non-empty prefix and no digit around the
    id, so every code is reversible to its raw id."""
    if not isinstance(fmt, str) or fmt.count("{}") != 1 or fmt.count("{") != 1 or fmt.count("}") != 1:
        raise ConfigError(f"codes.{kind}: {fmt!r} must hold exactly one {{}} for the raw id")
    prefix, suffix = fmt.split("{}")
    if not prefix or any(ch.isdigit() for ch in prefix + suffix):
        raise ConfigError(f"codes.{kind}: {fmt!r} needs a non-empty prefix and no digit around the id, so the code "
                          "is reversible")
    return prefix, suffix


def _check_codes(cfg: dict) -> None:
    seen = {}
    for kind in CODE_KINDS:
        pair = _split(cfg["codes"][kind], kind)
        if pair in seen:
            raise ConfigError(f"codes.{seen[pair]} and codes.{kind}: two kinds share the format "
                              f"{cfg['codes'][kind]!r}; a code must name its kind")
        seen[pair] = kind


def code(cfg: dict, kind: str, raw_id) -> str:
    """The display code of a dataset id: `codes.<kind>` with the raw id in place of {} (D-02)."""
    if kind not in CODE_KINDS:
        raise ConfigError(f"codes.{kind}: not a code kind ({', '.join(CODE_KINDS)})")
    if not isinstance(raw_id, str) or not RAW_ID.fullmatch(raw_id):
        raise ConfigError(f"codes.{kind}: {raw_id!r} is not a dataset id (the text of a whole number)")
    prefix, suffix = _split(cfg["codes"][kind], kind)
    return f"{prefix}{raw_id}{suffix}"


def raw_id(cfg: dict, kind: str, display: str) -> str:
    """The dataset id of a display code; the inverse of `code`."""
    if kind not in CODE_KINDS:
        raise ConfigError(f"codes.{kind}: not a code kind ({', '.join(CODE_KINDS)})")
    prefix, suffix = _split(cfg["codes"][kind], kind)
    if isinstance(display, str) and display.startswith(prefix) and display.endswith(suffix):
        inner = display[len(prefix):len(display) - len(suffix)]
        if RAW_ID.fullmatch(inner):
            return inner
    raise ConfigError(f"codes.{kind}: {display!r} is not a code of the form {cfg['codes'][kind]!r}")


# --------------------------------------------------------------------------------------------------- categories (D-33)

def _check_categories(cfg: dict, profile: dict | None) -> None:
    """The level is a category level of the dataset; the default row holds every row field; an override holds only
    row fields and names a code the profile saw (when checked); every distinct row passes the engine's self-check."""
    from .public import CATEGORY_LEVELS  # data.public imports this module; resolved at call time

    cats = cfg["categories"]
    if cats.get("level") not in CATEGORY_LEVELS:
        raise ConfigError(f"categories.level: {cats.get('level')!r} is not one of {', '.join(CATEGORY_LEVELS)}")
    default = cats.get("default")
    if not isinstance(default, dict):
        raise ConfigError("categories.default: the default row is required")
    missing, unknown = sorted(set(ROW_FIELDS) - default.keys()), sorted(default.keys() - set(ROW_FIELDS))
    if missing:
        raise ConfigError(f"categories.default: missing {', '.join(missing)}")
    if unknown:
        raise ConfigError(f"categories.default.{unknown[0]}: not a category row field ({', '.join(ROW_FIELDS)})")
    seen = None if profile is None else profile["categories"].get(cats["level"], None)
    rows = {"default": default}
    for raw, override in cats["overrides"].items():
        where = f"categories.overrides.{raw}"
        extra = sorted(override.keys() - set(ROW_FIELDS))
        if extra:
            raise ConfigError(f"{where}.{extra[0]}: not a category row field ({', '.join(ROW_FIELDS)})")
        if profile is not None and (seen is None or raw not in seen):
            raise ConfigError(f"{where}: the profile never saw {cats['level']} code {raw!r}")
        rows[f"overrides.{raw}"] = {**default, **override}
    for name, row in rows.items():
        _check_row(cfg, f"categories.{name}", row)


def _check_row(cfg: dict, where: str, row: dict) -> None:
    for field in ("shelf_life_days", "order_case", "delivery_cycle_days"):
        value, low = row[field], 0 if field == "shelf_life_days" else 1
        if not isinstance(value, int) or isinstance(value, bool) or value < low:
            raise ConfigError(f"{where}.{field}: {value!r} must be a whole number of at least {low}")
    if not isinstance(row["zone"], str) or not row["zone"]:
        raise ConfigError(f"{where}.zone: a storage zone is required")
    price = row["price"]
    if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float))):
        raise ConfigError(f"{where}.price: {price!r} must be a number, or null for no usable base price (D-35)")
    adapter = _adapter(cfg, where, row["schedule"])
    config.schedule(adapter, row["schedule"])
    if price is None:
        return  # no money figure for this row (D-35); its other fields are checked above
    try:
        config.self_check(adapter, Product(where, *(row[name] for name in config.PRODUCT_FIELDS)))
    except InputError as e:
        raise InputError(f"{where}: {e}") from e


def _adapter(cfg: dict, sku: str, schedule_name: str) -> dict:
    """The small cfg data.config's checks read: reference/policy.json with the public shortlist step, and the row's
    schedule as the entry of `sku`."""
    pj = cfg["policy_json"]
    engine = {**pj["engine_policy"], "shortlist_step": cfg["policy"]["shortlist_step"]}
    return {"policy": {**pj, "engine_policy": engine, "product_schedule": {sku: schedule_name}}}


# --------------------------------------------------------------------------------------------------- geography (D-29..D-31)

def _check_geography(cfg: dict, typed: list[tuple[str, dict]], profile: dict | None) -> None:
    """Every required leaf exists; every leaf is an Assumption except the service limit and k (Policy); the travel
    source is one data/runs.py implements; candidates stay in one city; no cost is named as a filter; the unmapped
    stores are distinct store ids the profile saw (when checked); every category zone has a receiving capacity."""
    geo = cfg["geography"]
    for path in GEOGRAPHY_REQUIRED:
        node = geo
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                raise ConfigError(f"geography.{path}: required reference value is missing")
            node = node[key]
    for path, node in typed:
        expected = "Policy" if path in GEOGRAPHY_POLICY else "Assumption"
        if node["type"] != expected:
            raise ConfigError(f"{path}: must be typed {expected}, not {node['type']!r} (D-30, D-34)")
    if geo["travel"]["source"] not in TRAVEL_SOURCES:
        raise ConfigError(f"geography.travel.source: {geo['travel']['source']!r} is not a travel-time source "
                          f"data/runs.py implements ({', '.join(TRAVEL_SOURCES)})")
    if geo["service"]["same_city"] is not True:
        raise ConfigError("geography.service.same_city: candidates stay in one city (D-30; spec §5 Transfers stay "
                          "within a city)")
    filters = geo["service"]["filters"]
    if not isinstance(filters, list) or not all(isinstance(f, str) for f in filters):
        raise ConfigError("geography.service.filters: must be a list of setting names")
    costly = [f for f in filters if f.startswith("costs") or "cost" in f]
    if costly:
        raise ConfigError(f"geography.service.filters: {', '.join(costly)} is a cost; transport cost is never a "
                          "filter, the engine weighs it against keep and markdown (D-29)")
    unmapped = geo["coverage"]["unmapped"]
    if not isinstance(unmapped, list) or not all(isinstance(s, str) and RAW_ID.fullmatch(s) for s in unmapped):
        raise ConfigError(f"geography.coverage.unmapped: {unmapped!r} must be a list of raw store ids (text)")
    twice = sorted({s for s in unmapped if unmapped.count(s) > 1}, key=int)
    if twice:
        raise ConfigError(f"geography.coverage.unmapped: {', '.join(twice)} listed twice")
    if profile is not None:
        unknown = sorted(set(unmapped) - profile["stores"], key=int)
        if unknown:
            raise ConfigError(f"geography.coverage.unmapped: {', '.join(unknown)} is not a store code in the profile")
    capacity = geo["capacity"]
    zones = {cfg["categories"]["default"]["zone"]} | {
        row["zone"] for row in cfg["categories"]["overrides"].values() if "zone" in row}
    for zone in sorted(zones):
        if zone not in capacity:
            raise ConfigError(f"geography.capacity: no receiving capacity for zone {zone!r}, a category row's zone")


def network_params(cfg: dict) -> NetworkParams:
    """The local candidate network's parameters from reference/geography.json (D-29..D-31, D-34). The unmapped stores
    come back as display codes, the codes the network stage keys stores by."""
    g = cfg["geography"]
    return NetworkParams(
        city_radius_km=g["positions"]["city_radius_km"], speed_kmh=g["travel"]["speed_kmh"],
        handling_min=g["travel"]["handling_min"], max_travel_min=g["service"]["max_travel_min"], k=g["runs"]["k"],
        run_days=tuple(g["runs"]["days"]), loading_cutoff_min=g["runs"]["loading_cutoff_min"],
        departs_min=g["runs"]["departs_min"], spare_totes=g["runs"]["spare_totes"],
        cold_chain_share=g["runs"]["cold_chain_share"], fixed_cost_base=g["costs"]["fixed_base"],
        per_km_cost=g["costs"]["per_km"], per_unit_cost=g["costs"]["per_unit"], cancel_cost=g["costs"]["cancel"],
        capacity_totes=dict(g["capacity"]),
        unmapped=frozenset(code(cfg, "store", s) for s in g["coverage"]["unmapped"]), seed=g["positions"]["seed"])


def fingerprint(ref_in: pathlib.Path | str = REF_IN) -> str:
    """'ref-' + the first 12 hex of sha256 over public.json, categories.json, geography.json and policy.json, in that
    order, each as its name and its canonical JSON (data.config.fingerprint's rule): layout and key order never count,
    any value or source does, except the NOT_FINGERPRINTED blocks (run-time guards, not inputs of the set)."""
    ref_in = pathlib.Path(ref_in)
    digest = hashlib.sha256()
    for name in FINGERPRINTED:
        tree = config.read(ref_in / f"{name}.json")
        if isinstance(tree, dict):
            tree = {k: v for k, v in tree.items() if k not in NOT_FINGERPRINTED.get(name, ())}
        digest.update(f"{name}\n{config.canonical(tree)}\n".encode("utf-8"))
    return "ref-" + digest.hexdigest()[:12]


# --------------------------------------------------------------------------------------------------- products

def category_row(cfg: dict, product_row: dict) -> dict:
    """The product's category row: the default row overlaid by the override for its code at `categories.level`."""
    level = cfg["categories"]["level"]
    raw = product_row.get(level)
    if raw is None:
        raise ConfigError(f"categories.level: the product row has no {level!r} code")
    return {**cfg["categories"]["default"], **cfg["categories"]["overrides"].get(str(raw), {})}


def money_available(cfg: dict, product_row: dict) -> bool:
    """False when the product's category row has no usable base price: its financial comparisons are unavailable."""
    return category_row(cfg, product_row)["price"] is not None


def _product(cfg: dict, product_row: dict, row: dict) -> Product:
    return Product(code(cfg, "product", product_row["product"]), *(row[name] for name in config.PRODUCT_FIELDS))


def product(cfg: dict, product_row: dict) -> Product:
    """The engine Product: sku = the formatted product code, the money and tote fields from the category row. A row
    without a base price is refused: no money figure is made for it (D-35)."""
    row = category_row(cfg, product_row)
    if row["price"] is None:
        raise ConfigError(f"product {code(cfg, 'product', product_row['product'])}: its category row has no usable "
                          "base price, so its financial comparisons are unavailable; full price never stands in for a "
                          "missing amount (D-35)")
    return _product(cfg, product_row, row)


def schedule_for(cfg: dict, product_row: dict) -> tuple[tuple[int, int], ...]:
    """The product's markdown schedule: its category row's named schedule in reference/policy.json."""
    return config.schedule({"policy": cfg["policy_json"]}, category_row(cfg, product_row)["schedule"])


def policy(cfg: dict):
    """The engine Policy: reference/policy.json engine_policy with `policy.shortlist_step` replaced (D-34)."""
    return dataclasses.replace(config.engine_policy({"policy": cfg["policy_json"]}),
                               shortlist_step=cfg["policy"]["shortlist_step"])


def markdown(cfg: dict):
    """The store's assumed markdown response from reference/policy.json."""
    return config.markdown_response({"policy": cfg["policy_json"]})


def stock_params(cfg: dict, product_row: dict) -> StockParams:
    """The stock simulation's parameters for one product (D-33, D-39). A row without a base price still simulates
    stock (only money is unavailable, D-35), so its Product carries price None."""
    row, st, cal = category_row(cfg, product_row), cfg["stock"], cfg["calendar"]
    return StockParams(
        product=_product(cfg, product_row, row), schedule=schedule_for(cfg, product_row),
        shelf_life_days=row["shelf_life_days"], cycle_days=row["delivery_cycle_days"], order_case=row["order_case"],
        multiplier=tuple(st["multiplier"]), lookback_days=st["lookback_days"],
        same_weekday_obs=st["same_weekday_obs"], error_share=st["error_share"], error_range=tuple(st["error_range"]),
        error_from_day=st["error_from_day"], expiry_estimated_share=st["expiry_estimated_share"],
        first_day=cal["first_day"], last_day=cal["last_day"], delivery_min=cal["delivery_min"], seed=st["seed"],
        topup_on_eval_days=st["topup_on_eval_days"])


def settings(cfg: dict) -> dict:
    """The block the manifest carries for the app: calendar, mark, currency, case opening, evaluation, board,
    catalogue, series, the warm-up price response and the forecast settings."""
    return {"calendar": dict(cfg["calendar"]), "mark": dict(cfg["mark"]),
            "currency": cfg["policy_json"]["settings"]["currency"], "case_opening": dict(cfg["case_opening"]),
            "evaluation": dict(cfg["evaluation"]), "board": dict(cfg["board"]), "catalogue": dict(cfg["catalogue"]),
            "series": dict(cfg["series"]), "truth": {"price_response": dict(cfg["truth"]["price_response"])},
            "forecast": dict(cfg["forecast"])}


# --------------------------------------------------------------------------------------------------- assumptions table

def assumptions_table(ref_in: pathlib.Path | str = REF_IN) -> list[tuple[str, object, str, str]]:
    """(path, value, unit, source) of every leaf typed Assumption in public.json, categories.json and geography.json,
    sorted by path (05.2-20 puts it in DATASET.md)."""
    ref_in = pathlib.Path(ref_in)
    typed = config.leaves(config.read(ref_in / "public.json")) + _companions(ref_in)[1]
    return sorted((path, node["value"], node.get("unit", ""), node["source"]) for path, node in typed
                  if node["type"] == "Assumption")


def _cell(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text.replace("|", "\\|").replace("\n", " ")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="frn.py", description="the public set's assumptions, from reference data")
    sub = parser.add_subparsers(dest="command", required=True)
    table = sub.add_parser("assumptions", help="list every leaf typed Assumption")
    table.add_argument("--md", action="store_true", help="print a Markdown table")
    args = parser.parse_args(argv)
    rows = assumptions_table()
    if args.md:
        print("| Path | Value | Unit | Source |")
        print("|---|---|---|---|")
        for row in rows:
            print("| " + " | ".join(_cell(cell) for cell in row) + " |")
    else:
        for row in rows:
            print("\t".join(_cell(cell) for cell in row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
