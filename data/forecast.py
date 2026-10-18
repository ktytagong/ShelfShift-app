"""Forecast runs (spec §5 Forecast; 03-CONTEXT D-05, D-08, D-10, D-19).

    python -m data.forecast --provider local                          # the local stand-in: development and tests only
    python -m data.forecast --provider ai_forecast [--project P]      # TimesFM; ARIMA_PLUS only for failing series
    python -m data.forecast --provider arima_plus [--project P]       # ARIMA_PLUS for every series (declared)
    python -m data.forecast --provider ai_forecast --preflight        # every check that needs no network
    python -m data.forecast --provider ai_forecast --dry-run          # load the input table, dry-run the query

The BigQuery providers (data/bq.py) run only on the user's machine with the user's credentials; the project comes
from --project or GOOGLE_CLOUD_PROJECT, never from code. `--provider local` refuses to replace a BigQuery forecast
whose basis_id still matches the seed (D-09: the stand-in never replaces a valid release forecast).

One run per demo day N uses data up to day N-1 and forecasts days N..horizon_end; one backtest run uses data up to
day -(backtest_days + 1) and forecasts days -backtest_days..-1. The input is estimated full-price-equivalent sales
(`data.preprocess.forecast_input`), never recovered true demand.

Every raw provider row is checked before anything else (D-07): an empty status, `low ≤ base ≤ high` exactly on
finite numbers, every required day present once, and confidence level 0.8. A triple that fails is never sorted,
swapped or clamped into shape. A series that fails falls back alone to the fallback provider, which is checked the
same way (D-08); if the fallback fails too, `build` raises `ForecastFailed` and the command writes diagnostics and
leaves the previous forecast.json byte-identical (D-09).

Every point names the provider that produced it; each run counts its series by provider, and `provider_summary`
recomputes the headline from those counts. There is no single provider for the whole file. A negative low, base or
high from a provider is set to 0 after its rows pass the checks, and the point keeps its `raw` triple and a
`clamped` flag, so a clamped interval is never shown as the provider's own (D-19).

`score` measures the backtest run against truth.json's latent full-price demand, the only thing truth.json is read
for, beside "same weekday last week" taken from the identical preprocessed input (D-06). The figures are accuracy
on synthetic data, and the comparison does not show that the preprocessing assumptions are correct.

The local seasonal-naive stand-in exists for development and tests only. It is never a release forecast, and the
release check refuses a file that contains it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Callable

from . import config
from . import preprocess
from . import versions

REF = pathlib.Path(__file__).resolve().parent / "reference"

LOCAL = "local-seasonal-naive"
AI_FORECAST = "ai_forecast"
ARIMA_PLUS = "arima_plus"
PROVIDERS = {
    AI_FORECAST: "TimesFM 2.5 via BigQuery AI.FORECAST",
    ARIMA_PLUS: "ARIMA_PLUS via BigQuery ML.FORECAST",
    LOCAL: "local seasonal-naive stand-in (development and tests only)",
}
PROVIDER_ORDER = (AI_FORECAST, ARIMA_PLUS, LOCAL)
Z80 = 1.2816  # two-sided 80% normal quantile, a statistical constant
TAGS = ("recorded", "converted", "imputed", "unfilled")
CHECK_LABELS = {
    "bounds": "interval checks",
    "status": "status checks",
    "coverage": "coverage checks",
    "confidence": "confidence-level checks",
}
INPUT_LABEL = "estimated full-price-equivalent sales"
LABEL = "Precomputed"
BACKTEST = "backtest"
LEVEL = 0.8  # spec §5: low and high are the 80% interval
CHECKS = ("status", "bounds", "coverage", "confidence")
DIAGNOSTICS = pathlib.Path("reports") / "forecast-diagnostics.json"
NEXT_STEP = "Correct the data or configuration, then rerun. The previous forecast.json is unchanged."


class ForecastFailed(ValueError):
    """A series failed its checks and no fallback produced valid rows for it (D-09). `.problems` lists every
    problem as {series_id, job, branch, sku, provider, check, day, detail}."""

    def __init__(self, problems: list[dict]):
        self.problems = problems
        super().__init__(f"{len({p['series_id'] for p in problems})} series failed every provider:\n"
                         + "\n".join(self.lines()))

    def lines(self) -> list[str]:
        """One line per failing series: each provider's first problem and how many more it had."""
        grouped = defaultdict(list)
        for p in self.problems:
            grouped[p["series_id"]].append(p)
        out = []
        for sid, ps in grouped.items():
            parts, providers = [], list(dict.fromkeys(p["provider"] for p in ps))
            for name in providers:
                mine = [p for p in ps if p["provider"] == name]
                more = f" (+{len(mine) - 1} more)" if len(mine) > 1 else ""
                parts.append(f"{name}: {mine[0]['check']} on day {mine[0]['day']}: {mine[0]['detail']}{more}")
            out.append(f"{sid} from " + "; then ".join(parts))
        return out


@dataclass(frozen=True)
class Provider:
    """`fetch(inputs_by_series, required_days_by_series)` returns {series_id: [row, ...]}, where a row is
    {day, low, base, high, confidence_level, status}, as the provider gave it (unclamped, unrounded)."""
    name: str
    fetch: Callable[[dict, dict], dict]


def series_id(job: str, branch: str, sku: str) -> str:
    return f"{job}|{branch}|{sku}"


def _split(sid: str) -> tuple[str, str, str]:
    job, branch, sku = sid.split("|")
    return job, branch, sku


def jobs(seed: dict) -> list[tuple[str, int, list[int]]]:
    """(job, cutoff, days): d0..d<last_demo_day> (cutoff N-1, days N..horizon_end), then the backtest run."""
    cal = seed["calendar"]
    out = [(f"d{n}", n - 1, list(range(n, cal["horizon_end"] + 1))) for n in range(cal["last_demo_day"] + 1)]
    bt = cal["backtest_days"]
    out.append((BACKTEST, -bt - 1, list(range(-bt, 0))))
    return out


def _series_rows(seed: dict) -> dict[tuple[str, str], list[dict]]:
    rows = defaultdict(list)
    for r in seed["daily_sales"]:
        rows[(r["branch"], r["sku"])].append(r)
    return {k: rows[k] for k in sorted(rows)}


def _bases(seed: dict) -> tuple[float, float]:
    md = seed["markdown_assumption"]
    return md["m20"][1], md["m30"][1]  # [low, base, high]: the base values (D-05)


def inputs(seed: dict) -> dict[str, list[tuple[int, float, str]]]:
    """Per series id: the D-05 forecast input up to that job's cutoff."""
    m20, m30 = _bases(seed)
    rows = _series_rows(seed)
    return {series_id(job, b, s): preprocess.forecast_input(rs, cutoff, m20, m30)
            for job, cutoff, _ in jobs(seed) for (b, s), rs in rows.items()}


def required(seed: dict) -> dict[str, list[int]]:
    """Per series id: the days its run must forecast."""
    keys = list(_series_rows(seed))
    return {series_id(job, b, s): list(days) for job, _, days in jobs(seed) for b, s in keys}


def local_fetch(inputs_by_series: dict, required_days: dict) -> dict:
    """Seasonal naive: the mean of the last 4 input values on the day's weekday; a Poisson-sized 80% interval.
    Rows are returned unclamped and unrounded, like any provider's."""
    out = {}
    for sid, days in required_days.items():
        series = inputs_by_series[sid]
        rows = []
        for d in days:
            same = [v for day, v, _ in series if day % 7 == d % 7][-4:]
            base = sum(same) / len(same)
            spread = Z80 * math.sqrt(max(base, 1.0))
            rows.append({"day": d, "low": base - spread, "base": base, "high": base + spread,
                         "confidence_level": 0.8, "status": ""})
        out[sid] = rows
    return out


LOCAL_PROVIDER = Provider(LOCAL, local_fetch)


def _run_id(job: str, cutoff: int, basis: str, by_series: dict) -> str:
    key = config.canonical({"job": job, "cutoff": cutoff, "basis_id": basis, "providers": by_series})
    return f"fr-{job}-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def _check_phrase(checks: Counter) -> str:
    ordered = sorted(checks.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ordered) == 1:
        return CHECK_LABELS.get(ordered[0][0], f"{ordered[0][0]} checks")
    return ", ".join(f"{CHECK_LABELS.get(c, f'{c} checks')} {n}" for c, n in ordered)


def provider_summary(runs: list[dict], points: list[dict]) -> dict:
    """Series-run counts by provider, recomputed from the points, for the demo runs and the backtest; the D-10
    headline names the provider of most demo series-runs and any fallback with the checks that failed."""
    backtest_ids = {r["forecast_run_id"] for r in runs if r["job"] == BACKTEST}
    series = {(p["forecast_run_id"], p["branch"], p["sku"], p["provider"]) for p in points}
    demo, back = Counter(), Counter()
    for run_id, _, _, provider in series:
        (back if run_id in backtest_ids else demo)[provider] += 1
    checks = Counter(f["failed_check"] for r in runs if r["job"] != BACKTEST for f in r["fallbacks"])
    total = sum(demo.values())
    order = {name: i for i, name in enumerate(PROVIDER_ORDER)}
    top = min(demo, key=lambda name: (-demo[name], order.get(name, len(order)), name)) if demo else None
    headline = f"{PROVIDERS.get(top, top)} for {demo[top]} of {total} series-runs" if top else "no demo series-runs"
    fell_back = sum(checks.values())
    if fell_back:
        headline += f"; ARIMA_PLUS fallback for {fell_back} (failed {_check_phrase(checks)})"
    return {
        "demo": {"series_runs": total, "by_provider": dict(sorted(demo.items())),
                 "fallback_checks": dict(sorted(checks.items()))},
        "backtest": {"series_runs": sum(back.values()), "by_provider": dict(sorted(back.items()))},
        "headline": headline,
    }


def _point(run_id: str, branch: str, sku: str, row: dict, provider: str) -> dict:
    raw = (row["low"], row["base"], row["high"])
    clamped = any(x < 0 for x in raw)
    low, base, high = (round(max(0.0, x), 2) for x in raw)
    point = {"forecast_run_id": run_id, "branch": branch, "sku": sku, "day": row["day"],
             "low": low, "base": base, "high": high, "provider": provider, "clamped": clamped}
    if clamped:
        point["raw"] = {"low": round(raw[0], 4), "base": round(raw[1], 4), "high": round(raw[2], 4)}
    return point


def assemble(seed: dict, chosen: dict, fallbacks: dict, level: float = LEVEL) -> dict:
    """The forecast artifact from `chosen` (series id -> (provider name, rows)) and `fallbacks` (series id ->
    {failed_check, detail}). Only each series' required days are taken; rows for other days are ignored."""
    basis = versions.basis_id(seed)
    need = required(seed)
    ins = inputs(seed)
    runs, points = [], []
    for job, cutoff, days in jobs(seed):
        sids = sorted(s for s in need if _split(s)[0] == job)
        by_series = {"|".join(_split(s)[1:]): chosen[s][0] for s in sids}
        run_id = _run_id(job, cutoff, basis, by_series)
        clamped = 0
        for sid in sids:
            provider, rows = chosen[sid]
            _, branch, sku = _split(sid)
            by_day = {}
            for row in rows:
                if row["day"] in need[sid]:
                    if row["day"] in by_day:
                        raise ValueError(f"forecast {sid} from {provider} has two rows for day {row['day']}")
                    by_day[row["day"]] = row
            for d in need[sid]:
                if d not in by_day:
                    raise ValueError(f"forecast {sid} from {provider} has no row for day {d}")
                point = _point(run_id, branch, sku, by_day[d], provider)
                clamped += point["clamped"]
                points.append(point)
        counts = Counter(tag for s in sids for _, _, tag in ins[s])
        runs.append({
            "forecast_run_id": run_id, "job": job,
            "demo_day": None if job == BACKTEST else int(job[1:]),
            "cutoff_day": cutoff, "days": [days[0], days[-1]],
            "configured_model": seed["forecast"]["model"], "confidence_level": level, "label": LABEL,
            "providers": dict(sorted(Counter(by_series.values()).items())),
            "fallbacks": sorted(({"branch": _split(s)[1], "sku": _split(s)[2], **fallbacks[s]}
                                 for s in sids if s in fallbacks), key=lambda f: (f["branch"], f["sku"])),
            "clamped_points": clamped, "reference_history_id": basis,
            "input_counts": {tag: counts.get(tag, 0) for tag in TAGS},
        })
    points.sort(key=lambda p: (p["forecast_run_id"], p["branch"], p["sku"], p["day"]))
    return {
        "label": LABEL, "input_label": INPUT_LABEL, "confidence_level": level,
        "input_version": seed["dataset"]["input_version"], "basis_id": basis,
        "runs": runs, "points": points, "provider_summary": provider_summary(runs, points),
    }


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def series_problems(rows: list[dict], required_days, level: float = LEVEL) -> list[dict]:
    """The D-07 problems of one series' raw provider rows, as {check, day, detail}, ordered by day then check.

    Only rows for required days count. Per row: a non-empty status fails `status`; a low, base or high that is not
    a finite number, or any order other than low ≤ base ≤ high (exact, no tolerance), fails `bounds`; a confidence
    level other than `level` fails `confidence`. Per required day: no row, or more than one, fails `coverage`.
    Nothing is sorted, swapped or clamped here; a negative value alone is not a problem (D-19)."""
    need = set(required_days)
    out, seen = [], Counter()
    for r in rows:
        day = r.get("day")
        if not isinstance(day, int) or day not in need:  # an unparseable day is no row: coverage reports it
            continue
        seen[day] += 1
        if r.get("status"):
            out.append({"check": "status", "day": day, "detail": str(r["status"])})
        low, base, high = r.get("low"), r.get("base"), r.get("high")
        triple = f"low {low!r}, base {base!r}, high {high!r}"
        if not all(_finite(x) for x in (low, base, high)):
            out.append({"check": "bounds", "day": day, "detail": f"{triple}: not a finite number"})
        elif not low <= base <= high:
            why = ("lower above upper" if low > high else
                   "value above upper" if base > high else "value below lower")
            out.append({"check": "bounds", "day": day, "detail": f"{triple}: {why}"})
        if r.get("confidence_level") != level:
            out.append({"check": "confidence", "day": day,
                        "detail": f"confidence_level {r.get('confidence_level')!r}, expected {level!r}"})
    for day in sorted(need):
        if seen[day] == 0:
            out.append({"check": "coverage", "day": day, "detail": f"missing: no row for day {day}"})
        elif seen[day] > 1:
            out.append({"check": "coverage", "day": day, "detail": f"duplicate: {seen[day]} rows for day {day}"})
    return sorted(out, key=lambda p: (p["day"], CHECKS.index(p["check"])))


def _named(problems_by_series: dict, provider: str) -> list[dict]:
    out = []
    for sid, problems in problems_by_series.items():
        job, branch, sku = _split(sid)
        out += [{"series_id": sid, "job": job, "branch": branch, "sku": sku, "provider": provider, **p}
                for p in problems]
    return out


def build(seed: dict, primary: Provider, fallback: Provider | None = None, level: float = LEVEL) -> dict:
    """Fetch every series from `primary`, check its raw rows (D-07), send only the failing series to `fallback`
    and check those rows the same way (D-08), then assemble, which clamps negatives (D-19). Each run records the
    series that fell back with the first check they failed. Raises `ForecastFailed` when a series has no valid
    rows from any provider (D-09)."""
    ins, req = inputs(seed), required(seed)
    first = primary.fetch(ins, req)
    bad = {sid: p for sid in sorted(req) if (p := series_problems(first.get(sid, []), req[sid], level))}
    chosen = {sid: (primary.name, first[sid]) for sid in req if sid not in bad}
    fallbacks = {}
    if bad:
        if fallback is None:
            raise ForecastFailed(_named(bad, primary.name))
        second = fallback.fetch({sid: ins[sid] for sid in bad}, {sid: req[sid] for sid in bad})
        still = {sid: p for sid in bad if (p := series_problems(second.get(sid, []), req[sid], level))}
        if still:
            raise ForecastFailed([q for sid in still for q in
                                  _named({sid: bad[sid]}, primary.name) + _named({sid: still[sid]}, fallback.name)])
        for sid, problems in bad.items():
            chosen[sid] = (fallback.name, second[sid])
            fallbacks[sid] = {"failed_check": problems[0]["check"], "detail": problems[0]["detail"]}
    return assemble(seed, chosen, fallbacks, level)


class CoverageError(ValueError):
    """The assembled forecast candidate is incomplete or inconsistent; the message names the first problem."""


def check_candidate(forecast: dict, seed: dict) -> None:
    """Check the whole assembled artifact before it may replace forecast.json (DATA-05, D-09): one run per job
    (d0..d<last_demo_day> and the backtest) with the seed calendar's cutoff and days; every branch × SKU × required
    day of every run exactly once; every point an ordered non-negative finite triple from an allowed provider;
    confidence 0.8; each run's provider and clamped counts and the provider summary recomputing from the points;
    and the basis id of this seed. Raises CoverageError with the first problem."""
    def fail(msg: str):
        raise CoverageError(msg)

    if forecast.get("basis_id") != versions.basis_id(seed):
        fail(f"forecast basis_id {forecast.get('basis_id')!r} is not this seed's {versions.basis_id(seed)!r}")
    if forecast.get("confidence_level") != LEVEL:
        fail(f"forecast confidence_level {forecast.get('confidence_level')!r}, expected {LEVEL!r}")
    expected = {job: (cutoff, days) for job, cutoff, days in jobs(seed)}
    runs = forecast.get("runs", [])
    by_job = defaultdict(list)
    for r in runs:
        if r.get("job") not in expected:
            fail(f"forecast run {r.get('forecast_run_id')} has an unknown job {r.get('job')!r}")
        by_job[r["job"]].append(r)
    by_id = {}
    for job, (cutoff, days) in expected.items():
        if not by_job[job]:
            fail("no forecast run for the backtest" if job == BACKTEST else f"no forecast run for demo day {job[1:]}")
        if len(by_job[job]) > 1:
            fail(f"{len(by_job[job])} forecast runs for {job}")
        r = by_job[job][0]
        rid = r.get("forecast_run_id")
        if rid in by_id:
            fail(f"forecast run id {rid} is used twice")
        if r.get("cutoff_day") != cutoff or r.get("days") != [days[0], days[-1]]:
            fail(f"forecast run {rid} has cutoff {r.get('cutoff_day')} and days {r.get('days')}; the seed calendar "
                 f"says cutoff {cutoff} and days {[days[0], days[-1]]}")
        if r.get("confidence_level") != LEVEL:
            fail(f"forecast run {rid} has confidence_level {r.get('confidence_level')!r}, expected {LEVEL!r}")
        by_id[rid] = (r, set(days))
    series = [(b, p["sku"]) for b in seed["branches"] for p in seed["products"]]
    known = set(series)
    seen = Counter()
    providers = defaultdict(set)
    clamped = Counter()
    for p in forecast.get("points", []):
        rid, branch, sku, day = p.get("forecast_run_id"), p.get("branch"), p.get("sku"), p.get("day")
        where = f"forecast run {rid}, {sku} at {branch} day {day}"
        if rid not in by_id:
            fail(f"a point names an unknown forecast run {rid!r} ({sku} at {branch} day {day})")
        if (branch, sku) not in known or day not in by_id[rid][1]:
            fail(f"{where} is outside the run's series and days")
        if p.get("provider") not in PROVIDERS:
            fail(f"{where} has an unknown provider {p.get('provider')!r}")
        low, base, high = p.get("low"), p.get("base"), p.get("high")
        if not (all(_finite(x) for x in (low, base, high)) and 0 <= low <= base <= high):
            fail(f"{where} is not an ordered non-negative triple: low {low!r}, base {base!r}, high {high!r}")
        seen[(rid, branch, sku, day)] += 1
        providers[(rid, branch, sku)].add(p["provider"])
        clamped[rid] += bool(p.get("clamped"))
    for rid, (r, days) in by_id.items():
        for branch, sku in series:
            for day in sorted(days):
                if seen[(rid, branch, sku, day)] == 0:
                    fail(f"forecast run {rid} has no {sku} at {branch} for day {day}")
                if seen[(rid, branch, sku, day)] > 1:
                    fail(f"forecast run {rid} has {seen[(rid, branch, sku, day)]} points for {sku} at {branch} "
                         f"for day {day}")
            if len(providers[(rid, branch, sku)]) > 1:
                fail(f"forecast run {rid} mixes providers {sorted(providers[(rid, branch, sku)])} for {sku} at "
                     f"{branch}")
        counts = dict(sorted(Counter(next(iter(providers[(rid, b, s)])) for b, s in series).items()))
        if r.get("providers") != counts:
            fail(f"forecast run {rid} providers {r.get('providers')} do not recompute from its points {counts}")
        if r.get("clamped_points") != clamped[rid]:
            fail(f"forecast run {rid} clamped_points {r.get('clamped_points')} do not recompute from its points "
                 f"{clamped[rid]}")
    if forecast.get("provider_summary") != provider_summary(runs, forecast["points"]):
        fail("forecast provider_summary does not recompute from its runs and points")


def run_forecasts(seed: dict, provider: str = LOCAL) -> dict:
    """Compatibility entry for Phase 8's weekcheck: the local stand-in only, for now."""
    if provider != LOCAL:
        raise ValueError(f"run_forecasts supports only {LOCAL!r} so far, got {provider!r}")
    return build(seed, LOCAL_PROVIDER)


COMPARISON_TAIL = (" versus same weekday last week, using identical preprocessed inputs; "
                   "evaluated against synthetic full-price demand.")
_ALONE = {AI_FORECAST: "TimesFM", ARIMA_PLUS: "ARIMA_PLUS", LOCAL: "The local seasonal-naive stand-in"}
_MIXED = {AI_FORECAST: "TimesFM 2.5", ARIMA_PLUS: "ARIMA_PLUS", LOCAL: "the local seasonal-naive stand-in"}
BIAS_DEFINITION = "Σ(forecast − actual) / Σ actual; positive means over-forecast"
SCORE_NOTE = ("A fair model comparison on identical inputs; it does not show that the preprocessing "
              "assumptions are correct (D-06).")


def comparison_label(providers: dict) -> str:
    """The D-06 label, naming the providers actually in the backtest run: one provider alone, or each with its
    series count."""
    order = {name: i for i, name in enumerate(PROVIDER_ORDER)}
    names = sorted((p for p, n in providers.items() if n), key=lambda p: (order.get(p, len(order)), p))
    if len(names) == 1:
        return _ALONE.get(names[0], names[0]) + COMPARISON_TAIL
    parts = [f"{_MIXED.get(p, p)} ({providers[p]} series)" for p in names]
    head = ", ".join(parts[:-1]) + " and " + parts[-1]
    return head[0].upper() + head[1:] + COMPARISON_TAIL


def _versus(model: float | None, baseline: float | None) -> str | None:
    if model is None or baseline is None:
        return None
    m, b = round(model, 4), round(baseline, 4)
    return "equal" if m == b else "better" if m < b else "worse"


def _figures(series: list[list[tuple]]) -> dict:
    """WAPE, bias and 80% coverage of the model and the baseline over the series given; each series is a list of
    (forecast point, actual, baseline value). None when their actual demand sums to 0."""
    rows = [x for s in series for x in s]
    total = sum(a for _, a, _ in rows)
    if not rows or total <= 0:
        return {"wape": None, "bias": None, "baseline_wape": None, "baseline_bias": None, "coverage_80": None}
    return {
        "wape": round(sum(abs(p["base"] - a) for p, a, _ in rows) / total, 4),
        "bias": round(sum(p["base"] - a for p, a, _ in rows) / total, 4),
        "baseline_wape": round(sum(abs(b - a) for _, a, b in rows) / total, 4),
        "baseline_bias": round(sum(b - a for _, a, b in rows) / total, 4),
        "coverage_80": round(sum(p["low"] <= a <= p["high"] for p, a, _ in rows) / len(rows), 4),
    }


def score(truth: dict, forecast: dict, seed: dict) -> dict:
    """Accuracy of the backtest run on synthetic data (DATA-04, D-06).

    Actuals are truth["demand"] (latent full-price demand) and nothing else; the baseline is "same weekday last
    week" read from the identical preprocessed input the model got (the latest same-weekday day at or before the
    cutoff). WAPE = Σ|f − a| / Σa and bias = Σ(f − a) / Σa over the series whose actual demand sums to more than 0;
    a series summing to 0 reports MAE only."""
    bt = next(r for r in forecast["runs"] if r["job"] == BACKTEST)
    _, cutoff, days = next(j for j in jobs(seed) if j[0] == BACKTEST)
    if bt["cutoff_day"] != cutoff or bt["days"] != [days[0], days[-1]]:
        raise ValueError(f"the backtest run (cutoff {bt['cutoff_day']}, days {bt['days']}) does not match the "
                         f"seed calendar (cutoff {cutoff}, days {[days[0], days[-1]]})")
    actual = {(t["branch"], t["sku"], t["day"]): t["demand"] for t in truth["demand"]
              if days[0] <= t["day"] <= days[-1]}
    model = {(p["branch"], p["sku"], p["day"]): p for p in forecast["points"]
             if p["forecast_run_id"] == bt["forecast_run_id"]}
    ins = {tuple(sid.split("|")[1:]): series for sid, series in inputs(seed).items()
           if sid.split("|")[0] == BACKTEST}
    scored, mae_only = {}, []
    for key in sorted(ins):
        rows = []
        for d in days:
            if (*key, d) not in model:
                raise ValueError(f"the backtest run has no point for {key[0]} {key[1]} day {d}")
            if (*key, d) not in actual:
                raise ValueError(f"truth has no demand for {key[0]} {key[1]} day {d}")
            same = [v for day, v, _ in ins[key] if day % 7 == d % 7]
            if not same:
                raise ValueError(f"no input on day {d}'s weekday at or before day {cutoff} for {key[0]} {key[1]}")
            rows.append((model[(*key, d)], actual[(*key, d)], same[-1]))
        if sum(a for _, a, _ in rows) > 0:
            scored[key] = rows
        else:
            mae_only.append({"series": list(key),
                             "mae": round(sum(abs(p["base"] - a) for p, a, _ in rows) / len(rows), 4)})

    def counts(keys) -> dict:
        c = Counter(tag for k in keys for _, _, tag in ins[k])
        return {tag: c.get(tag, 0) for tag in TAGS}

    pooled = _figures(list(scored.values()))
    per_sku = {}
    for product in seed["products"]:
        sku = product["sku"]
        figures = _figures([rows for k, rows in scored.items() if k[1] == sku])
        per_sku[sku] = {"wape": figures["wape"], "bias": figures["bias"],
                        "baseline_wape": figures["baseline_wape"], "baseline_bias": figures["baseline_bias"],
                        "model_vs_baseline": _versus(figures["wape"], figures["baseline_wape"]),
                        "input_counts": counts([k for k in ins if k[1] == sku])}
    providers = dict(sorted(Counter(provider for _, _, provider in
                                    {(p["branch"], p["sku"], p["provider"]) for p in model.values()}).items()))
    return {
        "window": [days[0], days[-1]], "cutoff_day": cutoff, "forecast_run_id": bt["forecast_run_id"],
        "providers": providers,
        **pooled,
        "model_vs_baseline": _versus(pooled["wape"], pooled["baseline_wape"]),
        "baseline": "same weekday last week", "bias_definition": BIAS_DEFINITION,
        "mae_only_series": mae_only, "input_counts": counts(list(ins)), "per_sku": per_sku,
        "label": "accuracy on synthetic data", "comparison_label": comparison_label(providers),
        "note": SCORE_NOTE,
    }


def dumps(forecast: dict) -> str:
    return json.dumps(forecast, indent=1, sort_keys=True) + "\n"


def write_forecast(path: pathlib.Path | str, forecast: dict) -> None:
    config.atomic_write(path, dumps(forecast))


BIGQUERY = (AI_FORECAST, ARIMA_PLUS)
NO_PROJECT = "set GOOGLE_CLOUD_PROJECT or pass --project"
REFUSE_LOCAL = "refusing to replace a valid BigQuery forecast with the local stand-in; pass --ref to write elsewhere"


def holds_valid_bigquery_forecast(path: pathlib.Path, seed: dict) -> bool:
    """True when `path` holds BigQuery points and its basis_id still equals this seed's (D-09). A forecast whose
    basis is stale (the sales history changed after a re-tune) is not valid for this seed and may be replaced."""
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return False
    points = existing.get("points") if isinstance(existing, dict) else None
    if not isinstance(points, list):
        return False
    from_bigquery = any(isinstance(p, dict) and p.get("provider") in BIGQUERY for p in points)
    return from_bigquery and existing.get("basis_id") == versions.basis_id(seed)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m data.forecast", description=__doc__.splitlines()[0])
    ap.add_argument("--provider", choices=["local", AI_FORECAST, ARIMA_PLUS], required=True,
                    help="ai_forecast: TimesFM with ARIMA_PLUS for failing series only; arima_plus: ARIMA_PLUS for "
                         "every series; local: the development stand-in")
    ap.add_argument("--ref", default=str(REF),
                    help="directory holding seed.json and truth.json; forecast.json is written there")
    ap.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                    help="Google Cloud project for the BigQuery providers (default: GOOGLE_CLOUD_PROJECT)")
    ap.add_argument("--dry-run", action="store_true",
                    help="load the input table and dry-run the AI.FORECAST query; print the bytes, write nothing")
    ap.add_argument("--preflight", action="store_true",
                    help="check the package, project, config, series, horizon and SQL without any network call")
    a = ap.parse_args(argv)
    ref = pathlib.Path(a.ref)
    seed = json.loads((ref / "seed.json").read_text(encoding="utf-8"))
    out = ref / "forecast.json"
    if a.provider == "local":
        if a.dry_run or a.preflight:
            ap.error("--dry-run and --preflight check a BigQuery provider; --provider local needs neither")
        if holds_valid_bigquery_forecast(out, seed):
            print(f"{out}: {REFUSE_LOCAL}", file=sys.stderr)
            sys.exit(2)
        primary, fallback = LOCAL_PROVIDER, None  # the stand-in has no fallback, and is never one (D-09)
    else:
        from . import bq  # imported here: data.bq imports this module
        if not a.project:
            print(NO_PROJECT, file=sys.stderr)
            sys.exit(2)
        if a.preflight:
            info = bq.preflight(seed, a.project, a.provider)
            text = info.pop("sql")
            print(json.dumps(info, indent=1))
            if text:
                print(text, end="")
            return 1 if info["problems"] else 0
        fc = seed["forecast"]
        try:
            ai, arima = bq.providers(a.project, fc["location"], fc["dataset"], fc["confidence_level"], fc["model"])
            if a.dry_run:
                if a.provider != AI_FORECAST:
                    ap.error("--dry-run checks the AI.FORECAST query; use it with --provider ai_forecast")
                ins, req = inputs(seed), required(seed)
                n = bq.dry_run(bq.make_client(a.project, fc["location"]), bq.Ids(a.project, fc["dataset"]), ins, req,
                               fc["confidence_level"], fc["model"])
                print(f"dry run: AI.FORECAST over {len(req)} series would process {n} bytes; {out} is unchanged")
                return 0
        except bq.BigQueryConfigError as e:
            print(f"BigQuery configuration refused: {e}", file=sys.stderr)
            sys.exit(2)
        primary, fallback = (ai, arima) if a.provider == AI_FORECAST else (arima, None)
    truth = json.loads((ref / "truth.json").read_text(encoding="utf-8"))
    try:
        forecast = build(seed, primary, fallback)
    except ForecastFailed as e:
        diag = ref / DIAGNOSTICS
        diag.parent.mkdir(parents=True, exist_ok=True)
        config.atomic_write(diag, json.dumps(
            {"provider": primary.name, "fallback": fallback.name if fallback else None,
             "problems": e.problems, "next_step": NEXT_STEP}, indent=1, sort_keys=True) + "\n")
        for line in e.lines():
            print(f"FAILED {line}", file=sys.stderr)
        print(f"wrote {diag}; {out} is unchanged. {NEXT_STEP.split('.')[0]}.", file=sys.stderr)
        sys.exit(1)
    forecast["quality"] = q = score(truth, forecast, seed)
    try:
        check_candidate(forecast, seed)  # the whole candidate, before the atomic replace
    except CoverageError as e:
        print(f"forecast candidate refused: {e}; {out} is unchanged", file=sys.stderr)
        sys.exit(1)
    write_forecast(out, forecast)
    clamped = sum(r["clamped_points"] for r in forecast["runs"])
    print(f"wrote {out}: {len(forecast['runs'])} runs, {len(forecast['points'])} points; "
          f"{forecast['provider_summary']['headline']}; clamped {clamped}; "
          f"WAPE {q['wape']} vs baseline {q['baseline_wape']}, bias {q['bias']} ({q['model_vs_baseline']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
