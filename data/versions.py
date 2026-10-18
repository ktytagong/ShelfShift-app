"""Version ids for the generated reference set (DATA-05; 03-RESEARCH "Versioning Scheme", Pitfall 10).

Three ids, each a prefix and the first 12 hex of sha256 over `config.canonical` JSON (sorted keys, compact, UTF-8):

- `input_version` ("in-") binds everything the world came from and everything it became: the reference fingerprint
  (every reference file, sources and the hidden response included), the seed.json body and the truth.json body,
  without their version fields. A change to any reference value or source, to the scenario, to the seed number or
  to the generator's output changes it. `data.seed` stamps it into seed.json and truth.json.
- `basis_id` ("fb-") binds exactly what a forecast reads from seed.json: the daily sales, the forecast calendar, the
  base of the store's assumed markdown response (the full-price-equivalent conversion, 03-CONTEXT D-05) and the
  forecast config. A quote, a capacity or a lot does not change it, so a forecast stays valid across them.
- `dataset_version` ("ds-") binds the released set: seed.json, forecast.json and truth.json, in that order. It
  needs a forecast, so it can never be part of an input the forecast is built from (no circularity).

No clock, no git and no environment value enters any hash, so the same inputs give the same ids in every process.
"""
from __future__ import annotations

import copy
import hashlib

from . import config

SEED_VERSION_FIELDS = ("input_version", "reference_fingerprint")  # in seed["dataset"]
TRUTH_VERSION_FIELDS = ("input_version",)


def digest(prefix: str, *parts) -> str:
    """prefix + the first 12 hex of sha256 over the canonical JSON array of `parts`."""
    return prefix + hashlib.sha256(config.canonical(list(parts)).encode("utf-8")).hexdigest()[:12]


def bodies(seed: dict, truth: dict) -> tuple[dict, dict]:
    """Copies of seed.json and truth.json without their version fields: what `input_version` is taken over."""
    seed, truth = copy.deepcopy(seed), copy.deepcopy(truth)
    for name in SEED_VERSION_FIELDS:
        seed.get("dataset", {}).pop(name, None)
    for name in TRUTH_VERSION_FIELDS:
        truth.pop(name, None)
    return seed, truth


def input_version(fingerprint: str, seed_body: dict, truth_body: dict) -> str:
    """'in-' + 12 hex over the reference fingerprint, the seed body and the truth body (version fields left out)."""
    seed_body, truth_body = bodies(seed_body, truth_body)
    return digest("in-", fingerprint, seed_body, truth_body)


def basis_id(seed: dict) -> str:
    """'fb-' + 12 hex over what the forecast reads: daily_sales, the forecast calendar, the m20/m30 base of the
    assumed markdown response and the forecast model and confidence level."""
    cal = seed["calendar"]
    md = seed["markdown_assumption"]
    basis = {
        "daily_sales": seed["daily_sales"],
        "calendar": {k: cal[k] for k in ("first_history_day", "last_demo_day", "horizon_end", "backtest_days")},
        "markdown_base": {"m20": md["m20"][1], "m30": md["m30"][1]},
        "forecast": {k: seed["forecast"][k] for k in ("model", "confidence_level")},
    }
    return digest("fb-", basis)


def dataset_version(seed: dict, forecast: dict, truth: dict) -> str:
    """'ds-' + 12 hex over seed.json, forecast.json and truth.json, in that order."""
    return digest("ds-", seed, forecast, truth)
