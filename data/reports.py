"""Reports for the record (D-16, D-04, D-11). Reported, never targets; nothing is tuned from them. Money is net revenue after operating costs (Seed calibration 8).

    python -m data.reports    # writes five_seed.json, init_sensitivity.json and release.json
                              # into data/reference/reports/

Every figure comes from the real generator's World internals (`data.seed.run_world`), never from a written file, and
never from the research scripts, which this module supersedes (03-CONTEXT D-14). The world is the do-nothing world:
no transfer happens in it, so transport and handling are 0 and net revenue after operating costs is revenue minus
disposal. Money is in the currency setting (USD).

Measures (03-CONTEXT D-16):
- history, series-days −90..−1 (branch × SKU × day): units received (the record quantity of every delivery arriving
  in the window), sold at full price, at 20% and at 30% off, wasted, the stockout share (lost > 0) and the
  half-day-or-more share (demand > 0 and lost ÷ demand ≥ 0.5), each share with its raw counts;
- case window, lots whose expiry day is 0 .. the last demo day: lots with stock left, the units left, and lots
  expiring empty, zeros included. Phase 3 cannot run the ESL-02 screen, so these stand in for open cases per demo
  day (`CASE_WINDOW_LABEL`);
- estimator, regular deliveries arriving −90..−1 only (designed lots log estimates over a different span; 03-04):
  the bias of the in-stock estimate against the true mean, the tier counts, the fallback count (tiers b and c) and
  the "initialization failed" count.

Pooled figures are recomputed from pooled counts and sums, never averaged from per-seed shares.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

from . import config, estimator, versions
from . import seed as S

ROOT = pathlib.Path(__file__).resolve().parent.parent  # the ShelfShift repo
REF = ROOT / "data" / "reference"
OUT = REF / "reports"
EPS = 1e-9
# the world generator's inputs: when any is uncommitted, the commit hash alone does not identify the generator
GENERATOR_PATHS = ("data/seed.py", "data/estimator.py", "data/world.py", "data/config.py", "data/versions.py",
                   "reference", "engine")

LABEL_NOTE = "Reported per seed and pooled; no pass/fail; nothing tuned (D-16)"
BENCHMARK_LABEL = "FreshRetailNet-50K; benchmarks, never targets"
CASE_WINDOW_LABEL = ("Lots in the case window with stock left stand in for open cases per demo day: Phase 3 cannot "
                     "run the ESL-02 screen, which Phase 4 builds. Phase 8's five-seed weekcheck reports open cases "
                     "per demo day, max and mean per seed (03-CONTEXT D-16 amended).")

HISTORY_UNITS = ("received", "sold_full", "sold_20", "sold_30", "wasted")
HISTORY_MONEY = ("revenue", "disposal", "transport_and_handling", "net_revenue_after_operating_costs")
HISTORY_COUNTS = ("series_days", "stockout_series_days", "half_day_or_more_series_days")


def _share(part: float, whole: float) -> float | None:
    return round(part / whole, 4) if whole else None


def _history_window(cfg: dict) -> tuple[int, int]:
    return -cfg["world"]["calendar"]["history_days"], -1


def _case_window(cfg: dict) -> tuple[int, int]:
    return 0, cfg["world"]["calendar"]["last_demo_day"]


def _history(counts: dict, units: dict, money: dict) -> dict:
    """The history block from its raw parts: shares recomputed from the counts, units and money rounded to 2."""
    return {**{k: counts[k] for k in HISTORY_COUNTS},
            **{k: round(units[k], 2) for k in HISTORY_UNITS},
            **{k: round(money[k], 2) for k in HISTORY_MONEY},
            "stockout_share": _share(counts["stockout_series_days"], counts["series_days"]),
            "half_day_or_more_share": _share(counts["half_day_or_more_series_days"], counts["series_days"])}


def _estimator(e_sum: float, mean_sum: float, tiers: dict, init_failed: int) -> dict:
    return {"e_sum": round(e_sum, 4), "mean_sum": round(mean_sum, 4),
            "bias_vs_true_mean": _share(e_sum - mean_sum, mean_sum),
            "tier_counts": {t: tiers[t] for t in estimator.TIERS},
            "fallback_count": tiers["b"] + tiers["c"], "init_failed_count": init_failed}


def world_measures(world, cfg: dict | None = None) -> dict:
    """The D-16 measures of one World (anything with `days`, `expired`, `deliveries` and `init_failed`)."""
    cfg = config.load() if cfg is None else cfg
    h_lo, h_hi = _history_window(cfg)
    c_lo, c_hi = _case_window(cfg)
    rows = [r for r in world.days if h_lo <= r["day"] <= h_hi]
    counts = {"series_days": len(rows),
              "stockout_series_days": sum(r["lost"] > EPS for r in rows),
              "half_day_or_more_series_days": sum(r["demand"] > 0 and r["lost"] / r["demand"] >= 0.5 for r in rows)}
    units = {"received": sum(r["qty"] for r in world.deliveries if h_lo <= r["day"] <= h_hi),
             **{k: sum(r[k] for r in rows) for k in ("sold_full", "sold_20", "sold_30", "wasted")}}
    revenue, disposal = sum(r["revenue"] for r in rows), sum(r["disposal"] for r in rows)
    money = {"revenue": revenue, "disposal": disposal, "transport_and_handling": 0.0,  # no transfer in this world
             "net_revenue_after_operating_costs": revenue - disposal}
    window = [e for e in world.expired if c_lo <= e["expiry_day"] <= c_hi]
    left = [e for e in window if e["qty_left"] > EPS]
    regular = [r for r in world.deliveries if r["kind"] == "regular" and h_lo <= r["day"] <= h_hi]
    tiers = dict.fromkeys(estimator.TIERS, 0)
    for r in regular:
        for tier in r["estimate"]["tiers"]:
            tiers[tier] += 1
    regular_keys = {(r["branch"], r["sku"], r["day"]) for r in regular}
    init_failed = sum((f["branch"], f["sku"], f["order_day"]) in regular_keys for f in world.init_failed)
    return {
        "history": _history(counts, units, money),
        "case_window": {"lots_with_stock_left": len(left),
                        "units_left": round(sum((e["qty_left"] for e in left), 0.0), 2),
                        "lots_expiring_empty": len(window) - len(left)},
        "estimator": _estimator(sum(r["estimate"]["e_sum"] for r in regular),
                                sum(r["estimate"]["mean_sum"] for r in regular), tiers, init_failed),
    }


def pool(measures: list[dict]) -> dict:
    """Sum the counts, units and money of several worlds' measures and recompute every share from the sums. Pooled
    units and money are the sums of the per-seed figures as reported."""
    h = [m["history"] for m in measures]
    cw = [m["case_window"] for m in measures]
    est = [m["estimator"] for m in measures]
    tiers = {t: sum(e["tier_counts"][t] for e in est) for t in estimator.TIERS}
    return {
        "history": _history({k: sum(x[k] for x in h) for k in HISTORY_COUNTS},
                            {k: sum(x[k] for x in h) for k in HISTORY_UNITS},
                            {k: sum(x[k] for x in h) for k in HISTORY_MONEY}),
        "case_window": {"lots_with_stock_left": sum(x["lots_with_stock_left"] for x in cw),
                        "units_left": round(sum((x["units_left"] for x in cw), 0.0), 2),
                        "lots_expiring_empty": sum(x["lots_expiring_empty"] for x in cw)},
        "estimator": _estimator(sum(e["e_sum"] for e in est), sum(e["mean_sum"] for e in est), tiers,
                                sum(e["init_failed_count"] for e in est)),
    }


def range_name(multiplier) -> str:
    lo, hi = multiplier
    return f"{lo:.2f}-{hi:.2f}"


def benchmarks(cfg: dict) -> dict:
    b = cfg["world"]["benchmarks"]
    return {"stockout_days": b["freshretailnet_stockout_days"],
            "half_day_or_more": b["freshretailnet_half_day_or_more"], "label": BENCHMARK_LABEL}


def _configuration(cfg: dict, world) -> dict:
    return {"opening_factor": world.params["opening_factor"],
            "burn_in_days": cfg["world"]["calendar"]["burn_in_days"], "schedules": world.params["schedules"],
            "history_days": list(_history_window(cfg)), "case_window_days": list(_case_window(cfg))}


def five_seed(cfg: dict | None = None, seeds=None) -> dict:
    """The evaluation scenario on every evaluation seed at both multiplier ranges, per seed and pooled (D-16, D-01)."""
    cfg = config.load() if cfg is None else cfg
    seeds = list(cfg["scenarios"]["evaluation"]["seeds"] if seeds is None else seeds)
    ordering = cfg["world"]["ordering"]
    ranges = {}
    for key, role in (("delivery_multiplier_release", "release (D-01)"),
                      ("delivery_multiplier_reported", "reported beside the release range (D-01)")):
        multiplier = list(ordering[key])
        worlds = {seed: S.run_world(seed, "evaluation", cfg, multiplier=multiplier) for seed in seeds}
        per_seed = {str(seed): world_measures(w, cfg) for seed, w in worlds.items()}
        ranges[range_name(multiplier)] = {"multiplier": multiplier, "role": role, "per_seed": per_seed,
                                          "pooled": pool(list(per_seed.values())),
                                          "configuration": _configuration(cfg, worlds[seeds[0]])}
    return {"scenario": "evaluation", "seeds": seeds, "ranges": ranges, "benchmarks": benchmarks(cfg),
            "currency": cfg["policy"]["settings"]["currency"], "note": LABEL_NOTE,
            "label": cfg["policy"]["settings"]["synthetic_label"], "case_window_label": CASE_WINDOW_LABEL}


def factor_name(factor: float) -> str:
    return str(float(factor))  # 0.8 -> "0.8", 1 -> "1.0"


def thirds(cfg: dict) -> list[tuple[int, int]]:
    """The history window in three consecutive parts (−90..−61, −60..−31, −30..−1); the last takes any remainder."""
    lo, hi = _history_window(cfg)
    size = (hi - lo + 1) // 3
    return [(lo, lo + size - 1), (lo + size, lo + 2 * size - 1), (lo + 2 * size, hi)]


def _stockouts_by_third(worlds: list, cfg: dict) -> dict:
    out = {}
    for lo, hi in thirds(cfg):
        rows = [r for w in worlds for r in w.days if lo <= r["day"] <= hi]
        n, k = len(rows), sum(r["lost"] > EPS for r in rows)
        out[f"{lo}..{hi}"] = {"series_days": n, "stockout_series_days": k, "stockout_share": _share(k, n)}
    return out


def init_sensitivity(cfg: dict | None = None, seeds=None) -> dict:
    """D-04: the final configuration at the low, configured and high opening factors on the evaluation seeds, the
    pooled stockout share by history third and the high − low gap. A consistency check; nothing is tuned from it."""
    cfg = config.load() if cfg is None else cfg
    seeds = list(cfg["scenarios"]["evaluation"]["seeds"] if seeds is None else seeds)
    ordering = cfg["world"]["ordering"]
    multiplier = list(ordering["delivery_multiplier_release"])
    factors = sorted({*map(float, ordering["init_sensitivity_factors"]), float(ordering["opening_factor"])})
    by_factor, worlds = {}, None
    for factor in factors:
        worlds = [S.run_world(seed, "evaluation", cfg, multiplier=multiplier, opening_factor=factor)
                  for seed in seeds]
        by_factor[factor_name(factor)] = _stockouts_by_third(worlds, cfg)
    low, high = factor_name(factors[0]), factor_name(factors[-1])
    gap = {t: round(by_factor[high][t]["stockout_series_days"] / by_factor[high][t]["series_days"]
                    - by_factor[low][t]["stockout_series_days"] / by_factor[low][t]["series_days"], 4)
           for t in by_factor[high]}
    configuration = _configuration(cfg, worlds[0])
    configuration.pop("opening_factor")  # it is what varies
    return {"scenario": "evaluation", "seeds": seeds, "multiplier": multiplier, "factors": by_factor,
            f"gap_{high}_minus_{low}": gap, "configuration": configuration,
            "label": cfg["policy"]["settings"]["synthetic_label"],
            "note": "A consistency check on the final configuration (case ordering on, each product's schedule "
                    "(A milk, B strawberries and bread), 90–100%, 56-day burn-in); nothing is tuned from it (D-04)."}


def _percent(share: float) -> str:
    return f"{share * 100:.1f}%"


def release(cfg: dict | None = None) -> dict:
    """D-11: the release seed's stockout days (X) and half-day-or-more (Y) over history −90..−1, reported beside the
    FreshRetailNet-50K benchmarks; not matched to them."""
    cfg = config.load() if cfg is None else cfg
    seed = cfg["scenarios"]["demo"]["seed"]
    multiplier = list(cfg["world"]["ordering"]["delivery_multiplier_release"])
    h = world_measures(S.run_world(seed, "demo", cfg, multiplier=multiplier), cfg)["history"]
    b = benchmarks(cfg)
    return {"scenario": "demo", "seed": seed, "multiplier": multiplier, "history_days": list(_history_window(cfg)),
            "stockout_days": h["stockout_share"], "half_day_or_more": h["half_day_or_more_share"],
            "counts": {k: h[k] for k in HISTORY_COUNTS}, "benchmarks": b,
            "label": f"The release seed's stockout days and half-day-or-more, reported beside FreshRetailNet-50K's "
                     f"{_percent(b['stockout_days'])} and {_percent(b['half_day_or_more'])} as benchmarks; not "
                     f"matched to them (D-11).",
            "note": "History days −90..−1 of the release (demo) world, with the designed lots and skips that fall "
                    "in that window.",
            "synthetic_label": cfg["policy"]["settings"]["synthetic_label"]}


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True,
                          timeout=10).stdout.strip()


def generator_commit() -> str:
    """The ShelfShift commit the report was generated on, with "-dirty" when a world generator input is uncommitted;
    informational, never hashed."""
    try:
        commit = _git("rev-parse", "--short", "HEAD")
        dirty = _git("status", "--porcelain", "--", *GENERATOR_PATHS)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable: {type(exc).__name__}"
    return commit + ("-dirty" if dirty else "")


def ids(ref: pathlib.Path = REF) -> dict:
    """The versions a report is tied to: the release seed's input_version and reference fingerprint, the
    dataset_version when the committed forecast was built on that seed (else None), and the generator commit."""
    seed = json.loads((ref / "seed.json").read_text(encoding="utf-8"))
    dataset_version = None
    if (ref / "forecast.json").exists():
        forecast = json.loads((ref / "forecast.json").read_text(encoding="utf-8"))
        if forecast.get("basis_id") == versions.basis_id(seed):
            truth = json.loads((ref / "truth.json").read_text(encoding="utf-8"))
            dataset_version = versions.dataset_version(seed, forecast, truth)
    return {"input_version": seed["dataset"]["input_version"],
            "reference_fingerprint": seed["dataset"]["reference_fingerprint"],
            "dataset_version": dataset_version, "generator_commit": generator_commit()}


def dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m data.reports", description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=pathlib.Path, default=OUT, help=f"output directory (default: {OUT})")
    args = parser.parse_args(argv)
    cfg = config.load()
    stamp = ids()
    built = {"five_seed": five_seed(cfg), "init_sensitivity": init_sensitivity(cfg), "release": release(cfg)}
    args.out.mkdir(parents=True, exist_ok=True)
    for name, report in built.items():
        config.atomic_write(args.out / f"{name}.json", dumps({**report, **stamp}))
    for name, r in built["five_seed"]["ranges"].items():
        h = r["pooled"]["history"]
        print(f"five_seed {name}: pooled stockout share {h['stockout_share']}, half-day-or-more "
              f"{h['half_day_or_more_share']}, wasted {h['wasted']} units")
    sens = built["init_sensitivity"]
    gap_key = next(k for k in sens if k.startswith("gap_"))
    print(f"init_sensitivity {gap_key} by third: " + ", ".join(f"{t} {v}" for t, v in sens[gap_key].items()))
    rel = built["release"]
    print(f"release seed {rel['seed']}: stockout days {rel['stockout_days']}, half-day-or-more "
          f"{rel['half_day_or_more']} (benchmarks {rel['benchmarks']['stockout_days']}, "
          f"{rel['benchmarks']['half_day_or_more']}; not targets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
