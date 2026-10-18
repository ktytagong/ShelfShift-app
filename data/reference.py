"""The reference set: seed.json + forecast.json (public) and truth.json (hidden), versioned together.

check() is the horizon-coverage gate; release_check() is the deploy gate (D-09). Both fail loudly; nothing is clamped.

    python -m data.reference [--ref DIR]              # load + check: prints "ok <dataset_version> (<headline>)"
    python -m data.reference [--ref DIR] --release    # the deploy gate: the local stand-in is refused

`dataset_version` ("ds-") is `versions.dataset_version` over the three files' canonical JSON, so it changes when any
part changes and never when only key order or indentation does (DATA-05). check() runs the forecast writer's own
whole-artifact rule (`forecast.check_candidate`), then requires hidden demand for every branch × SKU and capacity for
every branch × zone on every day 0..horizon end, and no lot or inbound row expiring after the horizon end (spec §5
Horizon coverage). release_check() adds what only a release needs: every point from BigQuery (ai_forecast or
arima_plus, never the local stand-in), the forecast built from this seed's sales history (`versions.basis_id`), the
seed generated from the reference/ being shipped (`reference_fingerprint` equal to `config.fingerprint()`), seed and
truth unchanged since generation (`input_version` recomputed over the fingerprint and both bodies, so truth is from
this seed's world), the seed calendar's run cutoffs, and the demo world on the reference demo seed. Phase 4's deploy
job calls release_check(load(ref_dir)) instead of comparing one provider name.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

from . import config
from . import forecast as F
from . import versions
from .forecast import CoverageError  # one class for the forecast writer and this gate

__all__ = ["REF", "CoverageError", "ReleaseError", "load", "check", "release_check", "main"]

REF = pathlib.Path(__file__).resolve().parent / "reference"
PARTS = ("seed", "forecast", "truth")  # the dataset_version order (data.versions)
ZONES = ("chilled", "ambient")  # the engine's two storage zones (engine.model.Product.zone)
RELEASE_PROVIDERS = (F.AI_FORECAST, F.ARIMA_PLUS)
RELEASE_SCENARIO = "demo"
RERUN = f"run python -m data.forecast --provider {F.AI_FORECAST}"
MOVED_NOTE = "the seed changed outside the forecast basis after the run; the forecast still matches its sales history"


class ReleaseError(ValueError):
    """The reference set is complete but may not ship: the message names the first reason (D-09)."""


def load(ref_dir: pathlib.Path | str = REF) -> dict:
    """{dataset_version, seed, forecast, truth} from seed.json, forecast.json and truth.json in `ref_dir`, read as
    UTF-8 (a repeated key, NaN or Infinity is refused, config.read)."""
    ref_dir = pathlib.Path(ref_dir)
    parts = {name: config.read(ref_dir / f"{name}.json") for name in PARTS}
    return {"dataset_version": versions.dataset_version(*(parts[name] for name in PARTS)), **parts}


def check(ref: dict) -> None:
    """The horizon-coverage gate (DATA-05, spec §5): raises CoverageError naming the first gap; nothing is clamped
    or filled in."""
    seed, truth = ref["seed"], ref["truth"]
    F.check_candidate(ref["forecast"], seed)  # runs, cutoffs, every point of every run, providers, basis
    end = seed["calendar"]["horizon_end"]
    series = [(b, p["sku"]) for b in seed["branches"] for p in seed["products"]]
    demand = {(t["branch"], t["sku"], t["day"]) for t in truth["demand"]}
    for b, s in series:
        for d in range(0, end + 1):
            if (b, s, d) not in demand:
                raise CoverageError(f"no hidden demand for {s} at {b} on day {d}")
    cap = {(c["branch"], c["zone"], c["day"]) for c in seed["capacity"]}
    for b in seed["branches"]:
        for z in ZONES:
            for d in range(0, end + 1):
                if (b, z, d) not in cap:
                    raise CoverageError(f"no {z} capacity for {b} on day {d}")
    for lot in seed["lots"] + seed["inbound"]:
        if lot["expiry_day"] > end:
            raise CoverageError(f"{lot.get('lot_id') or lot['inbound_id']} expires after the horizon end {end}")


def _points_from(forecast: dict, seed: dict, keep) -> list[dict]:
    """{series_id, day, provider} of the points whose provider `keep` accepts, in job order (d0..d<last>, then the
    backtest), then branch, SKU and day."""
    job_of = {r.get("forecast_run_id"): r.get("job") for r in forecast.get("runs", [])}
    order = {job: i for i, (job, _, _) in enumerate(F.jobs(seed))}
    found = []
    for p in forecast.get("points", []):
        if keep(p.get("provider")):
            job = str(job_of.get(p.get("forecast_run_id"), p.get("forecast_run_id")))
            branch, sku, day = str(p.get("branch")), str(p.get("sku")), p.get("day")
            key = (order.get(job, len(order)), branch, sku, day if isinstance(day, int) else 0)
            found.append((key, {"series_id": F.series_id(job, branch, sku), "day": day,
                                "provider": p.get("provider")}))
    return [point for _, point in sorted(found, key=lambda kv: kv[0])]


def _points(n: int) -> str:
    return f"{n} point" if n == 1 else f"{n} points"


def release_check(ref: dict) -> dict:
    """The deploy gate (D-09): raises ReleaseError (or CoverageError from check) with the first reason the set may
    not ship; returns {dataset_version, headline, seed_input_version, forecast_input_version, note}.

    The checks run in this order: the local stand-in, any other provider, the forecast basis (`basis_id`), check(),
    truth's input_version equal to seed.json's, seed.json's `reference_fingerprint` equal to `config.fingerprint()`,
    `input_version` recomputed over that fingerprint and both bodies, the run cutoffs, and the demo world. The
    provider scan and the basis come before check(), so a stand-in, a foreign provider or a forecast from another
    sales history is named as such rather than as the coverage problem it also causes. Both the fingerprint compare
    and the recompute are needed: the recompute leaves the stored fingerprint out (versions.bodies), so only the
    compare catches a rewritten one, and only the recompute catches a body edited under an unchanged fingerprint."""
    seed, fc, truth = ref["seed"], ref["forecast"], ref["truth"]
    local = _points_from(fc, seed, lambda name: name == F.LOCAL)
    if local:
        first = local[0]
        raise ReleaseError(f"refusing the local stand-in: {_points(len(local))} (first {first['series_id']} day "
                           f"{first['day']}); {RERUN}")
    foreign = _points_from(fc, seed, lambda name: name not in RELEASE_PROVIDERS)
    if foreign:
        first = foreign[0]
        names = ", ".join(sorted({repr(x["provider"]) for x in foreign}))
        raise ReleaseError(f"refusing provider {names}: {_points(len(foreign))} (first {first['series_id']} day "
                           f"{first['day']}); a release forecast comes only from {' or '.join(RELEASE_PROVIDERS)}")
    basis = versions.basis_id(seed)
    if fc.get("basis_id") != basis:
        raise ReleaseError(f"the forecast was built from another sales history: forecast.json has basis_id "
                           f"{fc.get('basis_id')!r}, this seed's is {basis!r}; {RERUN}")
    check(ref)
    seed_version = seed["dataset"]["input_version"]
    if truth.get("input_version") != seed_version:
        raise ReleaseError(f"truth.json is from another world: its input_version {truth.get('input_version')!r} "
                           f"is not seed.json's {seed_version!r}; regenerate both with python -m data.seed")
    fp = config.fingerprint()
    stored = seed["dataset"].get("reference_fingerprint")
    if stored != fp:
        raise ReleaseError(f"seed.json was generated from reference {stored!r}, reference/ is now {fp!r}; "
                           f"regenerate with python -m data.seed")
    recomputed = versions.input_version(fp, seed, truth)  # bodies() strips both version fields, so both checks
    if recomputed != seed_version:                       # are needed: the compare above, this recompute here
        raise ReleaseError(f"seed.json/truth.json bodies hash to {recomputed!r}, not their stamped {seed_version!r}; "
                           f"they were edited after generation; regenerate both with python -m data.seed")
    want = sorted((job, cutoff) for job, cutoff, _ in F.jobs(seed))
    got = sorted((r.get("job"), r.get("cutoff_day")) for r in fc["runs"])
    if got != want:
        raise ReleaseError(f"the forecast runs' cutoffs {got} are not the seed calendar's {want}")
    demo_seed = config.load()["scenarios"][RELEASE_SCENARIO]["seed"]
    scenario, number = seed["dataset"].get("scenario"), seed["dataset"].get("seed")
    if scenario != RELEASE_SCENARIO or number != demo_seed:
        raise ReleaseError(f"seed.json is the {scenario!r} world on seed {number!r}; a release is the "
                           f"{RELEASE_SCENARIO!r} world on the reference demo seed {demo_seed}")
    fc_version = fc.get("input_version")
    return {
        "dataset_version": ref["dataset_version"],
        "headline": fc["provider_summary"]["headline"],
        "seed_input_version": seed_version,
        "forecast_input_version": fc_version,
        "note": "" if fc_version == seed_version else MOVED_NOTE,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m data.reference", description=__doc__.splitlines()[0])
    ap.add_argument("--ref", default=str(REF), help="directory holding seed.json, forecast.json and truth.json")
    ap.add_argument("--release", action="store_true",
                    help="the deploy gate: also refuse the local stand-in, a forecast from another sales history, "
                         "truth from another world, a seed.json generated from another reference/ or edited after "
                         "generation, and any world other than the demo release seed")
    a = ap.parse_args(argv)
    try:
        ref = load(a.ref)
        if a.release:
            info = release_check(ref)
        else:
            check(ref)
            info = {"dataset_version": ref["dataset_version"],
                    "headline": ref["forecast"]["provider_summary"]["headline"], "note": ""}
    except (ValueError, KeyError, TypeError, OSError) as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    print(f"ok {info['dataset_version']} ({info['headline']})")
    if info["note"]:
        print(f"note: {info['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
