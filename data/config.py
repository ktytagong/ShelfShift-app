"""Reference data: every retailer, policy and world value, with its source and type (DATA-06, DATA-07).

Every value in `reference/*.json` is a leaf `{"value": ..., "source": "...", "type": "..."}` with an optional
`unit` and `note`. The type is one of Data, Policy, Adoptable, Reported, Assumption. A group is an object without a
"value" key. Nothing in `data/` keeps a retailer or policy value as a code constant: prices, costs, schedules,
multipliers, the waste price, the shortlist step, case sizes and the currency are read from here. The currency is a
setting (default USD in the seed) and is never defaulted in code.

The seeded Policy and every product's markdown schedule are checked by the engine's own input contract
(`engine.adjust.validate_input`), so a value the engine would refuse is the engine's `InputError` naming the field.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import tempfile

from engine.adjust import coerce_input, validate_input
from engine.model import ENGINE_VERSION, EngineInput, Forecast, Lot, MarkdownResponse, Policy, Product, schedule_problem

TYPES = ("Data", "Policy", "Adoptable", "Reported", "Assumption")
LEAF_KEYS = frozenset({"value", "source", "type"})
OPTIONAL_KEYS = frozenset({"unit", "note"})
REF_IN = pathlib.Path(__file__).resolve().parent.parent / "reference"
PUBLIC = ("policy", "world", "scenarios")  # the files load() returns; hidden.json is never among them
FINGERPRINTED = ("policy", "world", "scenarios", "hidden")
REQUIRED = ("policy.engine_policy", "policy.markdown_response.m20",
            "policy.markdown_response.m30", "policy.markdown_schedules.default", "policy.product_schedule",
            "world.calendar.last_demo_day", "world.branches", "world.products", "world.demand.base_demand",
            "world.demand.weekday_profile_raw", "world.capacity", "world.runs", "scenarios.demo.designed_lots",
            "scenarios.demo.skips", "scenarios.evaluation.designed_lots", "scenarios.evaluation.skips")
PRODUCT_FIELDS = ("zone", "price", "disposal_cost", "units_per_tote", "kg_per_unit")  # engine Product, after sku


class ConfigError(ValueError):
    pass


def read(path: pathlib.Path) -> dict:
    """One reference file as JSON, read as UTF-8. A repeated key, NaN or Infinity, or bad JSON is a ConfigError."""
    path = pathlib.Path(path)

    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:  # one dotted path can never hold two values
                raise ConfigError(f"{path.name}: duplicate key {key!r}")
            out[key] = value
        return out

    def finite_only(name):
        raise ConfigError(f"{path.name}: {name} is not a reference value")

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique, parse_constant=finite_only)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path.name}: not valid JSON ({e})") from None


def leaves(tree, prefix: str = "") -> list[tuple[str, dict]]:
    """Every leaf under `tree` as (dotted path, leaf), depth first in sorted key order. Rejects a malformed leaf or a
    group that is not a non-empty object of objects, naming the dotted path."""
    where = prefix or "<root>"
    if not isinstance(tree, dict) or not tree:
        raise ConfigError(f"{where}: a value must be a {{value, source, type}} leaf")
    if "value" in tree:
        missing = sorted(LEAF_KEYS - tree.keys())
        if missing:
            raise ConfigError(f"{where}: leaf is missing {', '.join(missing)}")
        unknown = sorted(tree.keys() - LEAF_KEYS - OPTIONAL_KEYS)
        if unknown:
            raise ConfigError(f"{where}: unknown leaf key(s) {', '.join(unknown)}")
        if not isinstance(tree["source"], str) or not tree["source"].strip():
            raise ConfigError(f"{where}: source must be a non-empty string")
        if tree["type"] not in TYPES:
            raise ConfigError(f"{where}: type {tree['type']!r} is not one of {', '.join(TYPES)}")
        for key in OPTIONAL_KEYS & tree.keys():
            if not isinstance(tree[key], str) or not tree[key].strip():
                raise ConfigError(f"{where}: {key} must be a non-empty string")
        return [(prefix, tree)]
    out: list[tuple[str, dict]] = []
    for key in sorted(tree):
        out.extend(leaves(tree[key], f"{prefix}.{key}" if prefix else key))
    return out


def values(tree):
    """The tree with every leaf replaced by its value."""
    if "value" in tree:
        return tree["value"]
    return {key: values(sub) for key, sub in tree.items()}


def load(ref_in: pathlib.Path | str = REF_IN) -> dict:
    """The public reference set {name: values} after checking every leaf, the currency setting and every schedule.

    Never returns the hidden response (reference/hidden.json is not in PUBLIC)."""
    ref_in = pathlib.Path(ref_in)
    cfg = {}
    for name in PUBLIC:
        tree = read(ref_in / f"{name}.json")
        leaves(tree, name)
        cfg[name] = values(tree)
    for path in REQUIRED:
        node = cfg
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                raise ConfigError(f"{path}: required reference value is missing")
            node = node[key]
    settings = cfg["policy"].get("settings")
    currency = settings.get("currency") if isinstance(settings, dict) else None  # DATA-07: never defaulted
    if not isinstance(currency, str) or not currency.strip():
        raise ConfigError("policy.settings.currency: the currency setting is required (DATA-07); it is never "
                          "defaulted in code")
    engine_policy(cfg)
    markdown_response(cfg)
    for name in cfg["policy"]["markdown_schedules"]:
        if name != "default":
            schedule(cfg, name)
    schedule(cfg)
    for sku in cfg["policy"]["product_schedule"]:
        product_schedule(cfg, sku)
    _cross_check(cfg)
    for product in products(cfg).values():
        self_check(cfg, product)
    return cfg


def _cross_check(cfg: dict) -> None:
    """The reference files agree: one schedule entry per product, and every branch and SKU a lot, skip, run,
    demand row or capacity row names is a known one."""
    world = cfg["world"]
    branches, skus = set(world["branches"]), set(world["products"])
    if set(cfg["policy"]["product_schedule"]) != skus:
        raise ConfigError(f"policy.product_schedule: keys {sorted(cfg['policy']['product_schedule'])} must equal "
                          f"world.products {sorted(skus)}")
    for scenario, sc in cfg["scenarios"].items():
        for group in ("designed_lots", "skips"):
            for key, row in sc[group].items():
                where = f"scenarios.{scenario}.{group}.{key}"
                if row.get("branch") not in branches:
                    raise ConfigError(f"{where}: unknown branch {row.get('branch')!r}")
                if row.get("sku") not in skus:
                    raise ConfigError(f"{where}: unknown sku {row.get('sku')!r}")
    for code, run in world["runs"].items():
        for end in ("origin", "dest"):
            if run["timetable"].get(end) not in branches:
                raise ConfigError(f"world.runs.{code}.timetable: unknown {end} {run['timetable'].get(end)!r}")
    if set(world["demand"]["base_demand"]) != skus:
        raise ConfigError(f"world.demand.base_demand: keys must equal world.products {sorted(skus)}")
    for sku, row in world["demand"]["base_demand"].items():
        if set(row) != branches:
            raise ConfigError(f"world.demand.base_demand.{sku}: keys must equal world.branches")
    for zone in {p["zone"] for p in world["products"].values()}:
        if set(world["capacity"].get(zone, {})) != branches:
            raise ConfigError(f"world.capacity.{zone}: keys must equal world.branches")


def products(cfg: dict) -> dict[str, Product]:
    """The engine Product for every SKU in world.products."""
    return {sku: Product(sku, *(p[name] for name in PRODUCT_FIELDS)) for sku, p in cfg["world"]["products"].items()}


def weekday_profile(cfg: dict) -> tuple[float, ...]:
    """The FreshRetailNet-derived weekday profile, Mon..Sun, divided by its own mean so it averages 1."""
    raw = cfg["world"]["demand"]["weekday_profile_raw"]
    if len(raw) != 7 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in raw):
        raise ConfigError("world.demand.weekday_profile_raw: must be 7 positive numbers, Mon..Sun")
    mean = sum(raw) / 7
    return tuple(v / mean for v in raw)


def horizon_end(cfg: dict) -> int:
    """The last day any demo-week lot can still sell: the last demo day plus the longest shelf life."""
    world = cfg["world"]
    return world["calendar"]["last_demo_day"] + max(p["shelf_life_days"] for p in world["products"].values())


def load_hidden(ref_in: pathlib.Path | str = REF_IN) -> dict:
    """The hidden true markdown response {"m20", "m30"}, for the generator only. Never merged into load()."""
    tree = read(pathlib.Path(ref_in) / "hidden.json")
    leaves(tree, "hidden")
    truth = values(tree)["markdown_truth"]
    return {"m20": truth["m20"], "m30": truth["m30"]}


def engine_policy(cfg: dict) -> Policy:
    """The engine Policy from `policy.engine_policy`: exactly one value per Policy field, no default filled in."""
    seeded = cfg["policy"]["engine_policy"]
    fields = {f.name for f in dataclasses.fields(Policy)}
    missing, unknown = sorted(fields - seeded.keys()), sorted(seeded.keys() - fields)
    if missing or unknown:
        raise ConfigError(f"policy.engine_policy: missing {missing or 'none'}, unknown {unknown or 'none'}; it must "
                          "hold every engine Policy field once")
    return Policy(**seeded)


def markdown_response(cfg: dict) -> MarkdownResponse:
    """The store's assumed markdown response (low, base, high) for 20% and 30%."""
    md = cfg["policy"]["markdown_response"]
    return MarkdownResponse(tuple(md["m20"]), tuple(md["m30"]))


def schedule(cfg: dict, name: str | None = None) -> tuple[tuple[int, int], ...]:
    """A named markdown schedule (default: `markdown_schedules.default`) as (days before expiry, pct) steps, sorted by
    days before expiry descending. Refused when empty (every product here is food) or when the engine's
    `schedule_problem` gives a reason (ESL-01)."""
    schedules = cfg["policy"]["markdown_schedules"]
    if name is None:
        name = schedules["default"]
    if not isinstance(name, str) or name == "default" or name not in schedules:
        raise ConfigError(f"markdown_schedules.{name}: no such schedule")
    raw = schedules[name]
    if isinstance(raw, (list, tuple)) and not raw:
        raise ConfigError(f"markdown_schedules.{name}: a food schedule must not be empty")
    steps = tuple(tuple(s) if isinstance(s, list) else s for s in raw) if isinstance(raw, (list, tuple)) else raw
    reason = schedule_problem(steps)
    if reason is not None:
        raise ConfigError(f"markdown_schedules.{name}: {reason}")
    return tuple(sorted(steps, reverse=True))


def product_schedule(cfg: dict, sku: str) -> tuple[tuple[int, int], ...]:
    """The markdown schedule a product follows: its `product_schedule` entry, else the default (03-CONTEXT D-20)."""
    return schedule(cfg, cfg["policy"]["product_schedule"].get(sku))


def self_check(cfg: dict, product: Product) -> None:
    """Run the engine's own input contract on the seeded Policy, markdown response and the product's schedule.

    Builds the smallest EngineInput validate_input accepts: one lot of the product at one placeholder branch, today 3
    at 09:00, forecasts for days 3..5. The engine's InputError propagates unchanged, naming the field.
    """
    branch, lot_id = "self-check", "SELF-CHECK"  # placeholders: only the seeded values are under test
    inp = EngineInput(ENGINE_VERSION, 3, 540, lot_id, product,
                      (Lot(lot_id, branch, 10.0, 5, 0),), (), (),
                      tuple(Forecast(branch, day, 0.0, 0.0, 0.0) for day in range(3, 6)), (), (),
                      markdown_response(cfg),
                      policy=engine_policy(cfg),
                      markdown_schedule=product_schedule(cfg, product.sku))
    validate_input(coerce_input(inp))


def canonical(obj) -> str:
    """Sorted keys, compact separators, UTF-8 text: the one serialization hashes are taken over."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def atomic_write(path: pathlib.Path | str, text: str) -> None:
    """Write `text` (UTF-8) to `path` through a temp file in the same directory, fsync, then os.replace, so an
    interrupted write leaves the previous file byte-identical."""
    path = pathlib.Path(path)
    tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent,
                                      prefix=f".{path.name}.", suffix=".tmp", delete=False)
    try:
        with tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp.name, path)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass
        raise


def fingerprint(ref_in: pathlib.Path | str = REF_IN) -> str:
    """'ref-' + the first 12 hex of sha256 over each existing reference file (policy, world, scenarios, hidden, in
    that order), each as its name and its canonical JSON."""
    ref_in = pathlib.Path(ref_in)
    digest = hashlib.sha256()
    for name in FINGERPRINTED:
        path = ref_in / f"{name}.json"
        if path.exists():
            digest.update(f"{name}\n{canonical(read(path))}\n".encode("utf-8"))
    return "ref-" + digest.hexdigest()[:12]
