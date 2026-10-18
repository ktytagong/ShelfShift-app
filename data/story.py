"""The Day-3 story check on the release world (D-15, D-18). It reads the record, never physical stock; the count enters
as a reported stock_count fact. Story beats are fixed; story numbers are whatever the release seed gives.

    python -m data.story [--ref DIR] [--out PATH] [--compare PATH]

For each Day-3 reviewer case (spec §5) the engine input is built from the record of the release world at 09:00 on
day 3: the lots at the start of day 3 plus that morning's 06:00 deliveries, which are lots at 09:00, not inbound
(03-CONTEXT D-21), with only the runs leaving the source that day (D-24), forecast run d3, the reference Policy and
the product's markdown schedule. `engine.evaluate` and `engine.fact_checks` run on it. SUS-19 (the lot tagged
milk-story) is evaluated again after a confirmed count of 0 for the phantom lot (tag milk-phantom), entered as a
`stock_count` reported fact (spec §7), never by editing the lot.

The report records D-15's four SUS-19 beats as booleans, each reviewer lot's first markdown-step day (D-18, asserted
to be day 4 by the CLI), the engine's fact checks and the competing lots of each reviewer branch × SKU (D-23), and the
numbers the docs quote with their "about" values. A failing beat is a recorded result: the CLI writes the report,
then exits 1. The SUS-19 re-tune within D-15's knobs is plan 03-14's, never this module's. The milk story is a
designed demo situation, never a typical case or evaluation evidence (Seed calibration 8).

Exit codes: 0 every beat holds; 1 a beat fails, Lat Phrao recorded milk sales on day 2, or a reviewer lot's first
step is not day 4; 3 `--compare` found a changed reviewer-case outcome (every beat holding).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import pathlib
import sys

from engine import evaluate, fact_checks
from engine.adjust import coerce_input, effective
from engine.limits import transfer_limit, unmet_by_day
from engine.model import (ENGINE_VERSION, KEEP, MARKDOWN, TRANSFER, Capacity, EngineInput, Forecast, Inbound, Lot,
                          Run)
from engine.timing import fmt_at, fmt_min

from . import config, versions
from . import forecast as F
from . import seed as S

ROOT = pathlib.Path(__file__).resolve().parent.parent  # the ShelfShift repo
REF = ROOT / "data" / "reference"
EPS = 1e-9
TODAY = 3  # the Day-3 reviewer cases open Thu (spec §5; D-15, D-18)
FIRST_STEP_DAY = 4  # every reviewer lot meets its first markdown step on Fri (D-18 amended, D-20)

# spec §5 reviewer targets, keyed by the designed lots' tags in reference/scenarios.json
CASES = {"milk-story": "transfer that a confirmed count changes",
         "strawberry-cold-chain": "every transfer rejected for cold chain; a markdown wins",
         "bread-keep": "Keep wins",
         "milk-close-call": "wins 2 of 3"}
STORY_TAG, PHANTOM_TAG = "milk-story", "milk-phantom"
STORY_RUN = "R-02"  # D-15 beat 4 names the run: "R-02 is usable"
BEATS = ("phantom_gap_visible", "count_changes_plan", "opens_thu_before_fri_step", "r02_usable")
COLD_CHAIN = "cold chain"  # engine.limits' reason text for a chilled product on a run without cold chain
LABEL = "Synthetic data · hypothetical scenario · designed demo situation"
DAY3_STATE_NOTE = (
    "The record at 09:00 on day 3: the lots at the start of day 3 plus that morning's 06:00 deliveries, which are "
    "lots at 09:00, not inbound (03-CONTEXT D-21). Only the runs leaving the source today are offered (D-24, a "
    "recorded limitation): on Thu the engine compares Keep against today's actions, so Keep's projection does not "
    "know about a later day's run; the case stays open and the next day's evaluation sees that day's runs. Lots are "
    "the record, never physical stock; a confirmed count enters only as a stock_count reported fact.")
NOTE = ("Engine projections on synthetic data with the forecast run named under forecast_providers; nothing here is "
        "observed. The milk story is a designed demo situation, never a typical case or evaluation evidence "
        "(D-15, Seed calibration 8).")


def about(x: float | None) -> int | None:
    """x rounded half up for "about" in the docs: to the nearest 10 from 100, the nearest 5 from 10, else whole."""
    if x is None:
        return None
    step = 10 if x >= 100 else 5 if x >= 10 else 1
    return step * math.floor(x / step + 0.5)


def first_step_day(expiry_day: int, schedule) -> int:
    """The first day the schedule marks the lot down: its expiry minus the schedule's largest days-before-expiry."""
    return expiry_day - max(k for k, _ in schedule)


def _seed_rows(seed_json: dict) -> dict[str, dict]:
    """Every lot and inbound row of seed.json by id: the public record's per-lot fields."""
    rows = {l["lot_id"]: l for l in seed_json["lots"]}
    rows.update({i["inbound_id"]: i for i in seed_json["inbound"]})
    return rows


def day3_lots(world: S.World, seed_json: dict, sku: str, today: int = TODAY) -> list[dict]:
    """The record of `sku` at 09:00 on `today`: the lots at the start of the day plus that day's 06:00 deliveries
    (lots at 09:00, 03-CONTEXT D-21), each with qty = the record (never physical stock). expiry_source,
    last_verified_day, origin and tag come from seed.json's rows; markdown_pct is the snapshot lot's percent, 0 for a
    06:00 delivery. A lot with no record left and no seed.json row is not stock and is left out."""
    if today not in world.snapshots:
        raise ValueError(f"the world has no snapshot at the start of day {today}")
    rows = _seed_rows(seed_json)
    found = [(l["lot_id"], l["branch"], float(l["recorded"]), l["expiry_day"], l["received_seq"], l["pct"])
             for l in world.snapshots[today] if l["sku"] == sku]
    found += [(r["lot_id"], r["branch"], float(r["qty"]), r["expiry_day"], r["received_seq"], 0)
              for r in world.deliveries if r["sku"] == sku and r["day"] == today and r["qty"] > 0]
    out = []
    for lot_id, branch, qty, expiry, seq, pct in found:
        row = rows.get(lot_id)
        if row is None:
            if qty > EPS:
                raise ValueError(f"{lot_id}: a lot with a record of {qty} is missing from seed.json")
            continue
        out.append({"lot_id": lot_id, "branch": branch, "sku": sku, "qty": qty, "expiry_day": expiry,
                    "received_seq": seq, "markdown_pct": pct, "expiry_source": row["expiry_source"],
                    "last_verified_day": row["last_verified_day"], "origin": row["origin"], "tag": row["tag"]})
    return sorted(out, key=lambda l: l["lot_id"])


def _run_points(forecast_json: dict, today: int) -> dict[tuple[str, str, int], tuple[float, float, float]]:
    """(branch, sku, day) -> (low, base, high) of forecast run d{today}."""
    run = next((r for r in forecast_json["runs"] if r["job"] == f"d{today}"), None)
    if run is None:
        raise ValueError(f"forecast.json has no run d{today}")
    return {(p["branch"], p["sku"], p["day"]): (p["low"], p["base"], p["high"])
            for p in forecast_json["points"] if p["forecast_run_id"] == run["forecast_run_id"]}


def build_input(world: S.World, seed_json: dict, forecast_json: dict, cfg: dict, lot_id: str,
                today: int = TODAY) -> EngineInput:
    """The day-`today` EngineInput for the case lot `lot_id`, from the record (master plan lines 9055-9080).

    Source = the case lot's branch; runs = the seed's runs leaving the source today (03-CONTEXT D-24); branches =
    the source and the run destinations; lots = the record at 09:00 of the SKU at those branches with qty > 0, plus
    the case lot; inbound = the seed's inbound of the SKU at those branches arriving after today through the case
    lot's expiry (today's 06:00 deliveries are lots, D-21); forecasts from run d{today}, days today..the last expiry;
    capacity at the destinations in the product's zone; sales yesterday from the recorded daily sales; markdown
    response, Policy and schedule from the reference set; the decision at the calendar's opening minute."""
    sku = next((l["sku"] for l in world.snapshots.get(today, ()) if l["lot_id"] == lot_id), None)
    if sku is None:
        sku = next((r["sku"] for r in world.deliveries if r["lot_id"] == lot_id), None)
    if sku is None:
        raise ValueError(f"{lot_id}: no such lot in the world")
    record = day3_lots(world, seed_json, sku, today)
    case = next((l for l in record if l["lot_id"] == lot_id), None)
    if case is None:
        raise ValueError(f"{lot_id}: not on the record at 09:00 on day {today}")
    source = case["branch"]
    product = config.products(cfg)[sku]
    runs = tuple(Run(r["run_code"], r["origin"], r["dest"], r["day"], r["loading_cutoff_min"], r["departs_min"],
                     r["arrives_min"], r["spare_totes"], r["cold_chain"], float(r["fixed_cost"]),
                     float(r["per_unit_cost"]), True,
                     None if r["cancel_cost"] is None else float(r["cancel_cost"]))
                 for r in sorted(seed_json["runs"], key=lambda r: r["run_code"])
                 if r["origin"] == source and r["day"] == today)
    dests = sorted({r.dest for r in runs})
    branches = sorted({source, *dests})
    lots = tuple(Lot(l["lot_id"], l["branch"], l["qty"], l["expiry_day"], l["received_seq"], l["markdown_pct"],
                     l["expiry_source"], l["last_verified_day"])
                 for l in record if l["branch"] in branches and (l["qty"] > EPS or l["lot_id"] == lot_id))
    inbound = tuple(Inbound(i["inbound_id"], i["branch"], float(i["qty"]), i["arrival_day"], i["arrival_min"],
                            i["expiry_day"], i["received_seq"])
                    for i in sorted(seed_json["inbound"], key=lambda i: i["inbound_id"])
                    if i["sku"] == sku and i["branch"] in branches and today < i["arrival_day"] <= case["expiry_day"])
    end = max([today] + [l.expiry_day for l in lots] + [i.expiry_day for i in inbound])
    points = _run_points(forecast_json, today)
    forecasts = tuple(Forecast(b, d, *points[(b, sku, d)]) for b in branches for d in range(today, end + 1)
                      if (b, sku, d) in points)
    free = {(c["branch"], c["zone"], c["day"]): c["free_totes"] for c in seed_json["capacity"]}
    capacity = tuple(Capacity(b, d, free[(b, product.zone, d)]) for b in dests for d in range(today, end + 1)
                     if (b, product.zone, d) in free)
    sales = tuple((r["branch"], float(r["recorded"])) for r in seed_json["daily_sales"]
                  if r["sku"] == sku and r["day"] == today - 1 and r["branch"] in branches)
    return EngineInput(ENGINE_VERSION, today, cfg["world"]["calendar"]["open_min"], lot_id, product, lots, inbound,
                       runs, forecasts, capacity, tuple(sorted(sales)), config.markdown_response(cfg),
                       policy=config.engine_policy(cfg), markdown_schedule=config.product_schedule(cfg, sku))


def evaluate_case(inp: EngineInput) -> dict:
    """The engine's case result and its fact checks: {"result": evaluate(inp), "fact_checks": fact_checks(...)}."""
    result = evaluate(inp)
    return {"result": result, "fact_checks": fact_checks(inp, result)}


def with_count(inp: EngineInput, lot_id: str, units: float, day: int) -> EngineInput:
    """The same input with one confirmed shelf + backroom count of `lot_id` as a stock_count reported fact."""
    return dataclasses.replace(inp, reported_facts=(
        {"kind": "stock_count", "lot_id": lot_id, "units": float(units), "counted_day": day},))


def _unmet(inp: EngineInput, branch: str, last_day: int) -> float | None:
    """Lost sales at `branch` in the base scenario if nothing is moved, days today..last_day (engine.limits)."""
    if branch not in {f.branch for f in inp.forecasts}:
        return None
    lost = unmet_by_day(effective(coerce_input(inp)), branch)
    return round(sum(v for d, v in lost.items() if inp.today <= d <= last_day), 2)


def _plan(result: dict) -> dict:
    """The recommended plan's row: key, label, kind, qty, dest, run_code, the scenario wins and the deadlines."""
    row = next(o for o in result["options"] if o["key"] == result["recommended"])
    return {"recommended": result["recommended"], "recommended_label": result["recommended_label"],
            "kind": row["kind"], "pct": row["pct"], "qty": row["qty"], "dest": row["dest"],
            "run_code": row["run_code"], "net": row["net"], "wasted_units": row["wasted_units"],
            "scenario_wins": result["scenario_wins"], "markdown_step": result.get("markdown_step"),
            "decide_by": result.get("decide_by"), "act_before": result.get("act_before"), "wait": result["wait"],
            "candidates_checked": result["candidates_checked"]}


def _keep_wasted(result: dict) -> float:
    return next(o["wasted_units"] for o in result["options"] if o["kind"] == KEEP)


def _transfer_qty(result: dict) -> int:
    row = next(o for o in result["options"] if o["key"] == result["recommended"])
    return row["qty"] if row["kind"] == TRANSFER else 0


def _checks(checks: list[dict]) -> list[dict]:
    keys = ("fact_id", "kind", "lot_ids", "pick_changed", "pick_feasible_if_alt", "changes_plan", "regret")
    return [{k: c[k] for k in keys} for c in checks]


def _rejected(result: dict) -> list[dict]:
    return [{"label": r["label"], "run_code": r["run_code"], "reasons": list(r["reasons"])}
            for r in result["rejected"]]


def _demo_line(result: dict, today: int) -> str | None:
    """'Decide by HH:MM today; the price drops P% tomorrow' from the engine's decide_by and markdown_step."""
    by, step = result.get("decide_by"), result.get("markdown_step")
    if by is None or step is None:
        return None
    at = by["day"] * 1440 + by["latest_decision_min"]
    when = f"{fmt_min(at)} today" if by["day"] == today else fmt_at(at, today)
    drop = "tomorrow" if step["days_until"] == 1 else f"in {step['days_until']} days"
    return f"Decide by {when}; the price drops {step['pct']}% {drop}"


def _target_met(tag: str, inp: EngineInput, result: dict, beats: dict | None, after: dict | None) -> bool:
    """Whether a reviewer case meets its spec §5 target (CASES); a target is reported, never forced."""
    kind = next(o["kind"] for o in result["options"] if o["key"] == result["recommended"])
    if tag == "milk-story":
        return bool(kind == TRANSFER and after is not None and after["kind"] == TRANSFER
                    and beats is not None and beats["count_changes_plan"])
    if tag == "strawberry-cold-chain":
        transfer_rows = [r for r in result["rejected"] if r["run_code"] is not None]
        every_run = {r.run_code for r in inp.runs} == {r["run_code"] for r in transfer_rows}
        return bool(inp.runs and every_run and all(COLD_CHAIN in r["reasons"] for r in transfer_rows)
                    and kind == MARKDOWN)
    if tag == "bread-keep":
        return kind == KEEP
    if tag == "milk-close-call":
        return result["scenario_wins"]["wins"] == 2
    raise ValueError(f"no reviewer target for tag {tag!r}")


def _load(ref_dir: pathlib.Path) -> tuple[dict, dict, dict]:
    def read(name: str) -> dict:
        return json.loads((ref_dir / name).read_text(encoding="utf-8"))
    return read("seed.json"), read("truth.json"), read("forecast.json")


def run(ref_dir: pathlib.Path | str = REF) -> dict:
    """The story report for the reference set in `ref_dir` (seed.json, truth.json, forecast.json).

    The world is regenerated from the reference files and must be the one seed.json records (same input_version),
    and the forecast must be built on this seed (same basis_id), else ValueError."""
    ref_dir = pathlib.Path(ref_dir)
    cfg = config.load()
    seed_json, truth, forecast_json = _load(ref_dir)
    dataset = seed_json["dataset"]
    if dataset["scenario"] != "demo":
        raise ValueError(f"the story check needs the demo world, got {dataset['scenario']!r}")
    fresh, _ = S.generate(dataset["seed"], "demo")
    if fresh["dataset"]["input_version"] != dataset["input_version"]:
        raise ValueError(f"seed.json is {dataset['input_version']}, but the reference files generate "
                         f"{fresh['dataset']['input_version']}: regenerate seed.json first")
    basis = versions.basis_id(seed_json)
    if forecast_json.get("basis_id") != basis:
        raise ValueError(f"forecast.json is built on {forecast_json.get('basis_id')}, not on this seed's {basis}")
    world = S.run_world(dataset["seed"], "demo", cfg)
    designed = cfg["scenarios"]["demo"]["designed_lots"]
    by_tag = {d["tag"]: lot_id for lot_id, d in designed.items()}
    story_id, phantom_id = by_tag[STORY_TAG], by_tag[PHANTOM_TAG]
    phantom_branch, milk = designed[phantom_id]["branch"], designed[story_id]["sku"]
    rows = _seed_rows(seed_json)

    # SUS-19 on the record and after a confirmed count of 0 for the phantom lot (D-15)
    inp = build_input(world, seed_json, forecast_json, cfg, story_id)
    case = inp.case_lot()
    on = evaluate_case(inp)
    counted = with_count(inp, phantom_id, 0.0, TODAY)
    after = evaluate_case(counted)
    rec, aft = on["result"], after["result"]
    phantom = next((c for c in on["fact_checks"] if c["fact_id"] == f"{phantom_branch}:phantom"), None)
    phantom_record = next((l.qty for l in inp.lots if l.lot_id == phantom_id), None)
    phantom_physical = truth["physical"].get(phantom_id)
    unmet_on = _unmet(inp, phantom_branch, case.expiry_day)
    unmet_after = _unmet(counted, phantom_branch, case.expiry_day)
    qty_on, qty_after = _transfer_qty(rec), _transfer_qty(aft)
    story_run = next((r for r in inp.runs if r.run_code == STORY_RUN), None)
    r02_reasons = (transfer_limit(effective(coerce_input(inp)), story_run)[1] if story_run is not None
                   else ["not offered today"])
    beats = {
        # the record shows the phantom lot at its branch while it is physically empty: the fact is visible and the
        # record understates that branch's shortfall (03-08 beat-1 reading, flagged for the user at 03-11)
        "phantom_gap_visible": bool(
            phantom is not None and phantom_id in phantom["lot_ids"] and phantom["recorded_value"] > 0
            and phantom_physical == 0 and unmet_on is not None and unmet_after is not None and unmet_after > unmet_on),
        "count_changes_plan": bool(phantom is not None and phantom["pick_changed"] and qty_after > qty_on),
        "opens_thu_before_fri_step": bool(
            rec.get("markdown_step") == {"day": FIRST_STEP_DAY, "pct": 20, "days_until": FIRST_STEP_DAY - TODAY}
            and rec.get("decide_by") is not None and rec["decide_by"]["day"] == TODAY),
        "r02_usable": story_run is not None and not r02_reasons,
    }
    window = [TODAY, case.expiry_day]

    def base_forecast(branch: str, last: int) -> float:
        return round(sum(f.base for f in inp.forecasts if f.branch == branch and TODAY <= f.day <= last), 2)

    free = [c.free_totes for c in inp.capacity if c.branch == phantom_branch and TODAY <= c.day <= case.expiry_day]
    numbers = {
        "case_lot_record_day3": round(case.qty, 2),
        "source_base_forecast_day3": base_forecast(case.branch, TODAY),
        "source_base_forecast_to_expiry": base_forecast(case.branch, case.expiry_day),
        "lat_phrao_record_day3": round(sum(l.qty for l in inp.lots if l.branch == phantom_branch), 2),
        "lat_phrao_base_forecast_day3": base_forecast(phantom_branch, TODAY),
        "lat_phrao_base_forecast_to_expiry": base_forecast(phantom_branch, case.expiry_day),
        "lat_phrao_free_totes_min": min(free) if free else None,
        "days_since_phantom_count": TODAY - rows[phantom_id]["last_verified_day"],
        "lat_phrao_unmet_on_record": unmet_on,
        "lat_phrao_unmet_after_count": unmet_after,
        "transfer_qty_on_record": qty_on,
        "transfer_qty_after_count": qty_after,
        "keep_wasted_units_on_record": _keep_wasted(rec),
        "keep_wasted_units_after_count": _keep_wasted(aft),
        "phantom_unserved_units": phantom["unserved_units"] if phantom else None,
    }
    wins_on, wins_after = rec["scenario_wins"]["wins"], aft["scenario_wins"]["wins"]
    sus19 = {
        "lots": {"case": story_id, "source": case.branch, "phantom": phantom_id, "phantom_branch": phantom_branch,
                 "window_days": window},
        "beats": beats,
        "on_record": _plan(rec),
        "after_count": _plan(aft),
        "count_fact": counted.reported_facts[0],
        "recommended_uses_r02": _plan(rec)["run_code"] == STORY_RUN,
        "r02_rejection_reasons": list(r02_reasons),
        "phantom_fact": None if phantom is None else {
            k: phantom[k] for k in ("fact_id", "lot_ids", "recorded_value", "pick_changed", "changes_plan", "regret",
                                    "decision_regret", "unserved_units", "best_plan_if_alt")},
        "phantom_physical": phantom_physical,
        "numbers": numbers,
        "about": {k: about(v) for k, v in numbers.items()},
        "demo_line": _demo_line(rec, TODAY),
        "wins_on_record": wins_on,
        "wins_after_count": wins_after,
        "video_phrase": (f"wins {wins_on} of 3 on the record and {wins_after} of 3 after the count" if wins_on < 3
                         else "wins 3 of 3"),
    }

    reviewer = {}
    for tag, target in CASES.items():
        lot_id = by_tag[tag]
        if lot_id == story_id:
            case_inp, out = inp, on
        else:
            case_inp = build_input(world, seed_json, forecast_json, cfg, lot_id)
            out = evaluate_case(case_inp)
        lot = case_inp.case_lot()
        sku = case_inp.product.sku
        schedule = config.product_schedule(cfg, sku)
        competing = [{"lot_id": l["lot_id"], "qty": round(l["qty"], 2), "expiry_day": l["expiry_day"],
                      "first_step_day": first_step_day(l["expiry_day"], schedule), "origin": l["origin"],
                      "tag": l["tag"]}
                     for l in day3_lots(world, seed_json, sku)
                     if l["branch"] == lot.branch and l["lot_id"] != lot_id and l["qty"] > EPS
                     and first_step_day(l["expiry_day"], schedule) <= FIRST_STEP_DAY]
        result = out["result"]
        reviewer[lot_id] = {
            "tag": tag, "branch": lot.branch, "sku": sku, "record_day3": round(lot.qty, 2),
            "expiry_day": lot.expiry_day,
            "schedule": [list(step) for step in schedule],
            "first_step_day": first_step_day(lot.expiry_day, schedule),
            "recommended": result["recommended"], "recommended_label": result["recommended_label"],
            "kind": next(o["kind"] for o in result["options"] if o["key"] == result["recommended"]),
            "run_code": next(o["run_code"] for o in result["options"] if o["key"] == result["recommended"]),
            "scenario_wins": result["scenario_wins"],
            "rejected": _rejected(result),
            "target": target,
            "target_met": _target_met(tag, case_inp, result, beats if tag == STORY_TAG else None,
                                      _plan(aft) if tag == STORY_TAG else None),
            "fact_checks": _checks(out["fact_checks"]),
            "competing_lots": competing,
        }

    d3 = next(r for r in forecast_json["runs"] if r["job"] == f"d{TODAY}")
    lp_day2 = next((r["recorded"] for r in seed_json["daily_sales"]
                    if r["branch"] == phantom_branch and r["sku"] == milk and r["day"] == TODAY - 1), None)
    return {
        "label": LABEL,
        "note": NOTE,
        "day3_state_note": DAY3_STATE_NOTE,
        "engine_version": ENGINE_VERSION,
        "input_version": dataset["input_version"],
        "basis_id": basis,
        "forecast_run_id": d3["forecast_run_id"],
        "forecast_providers": dict(sorted(d3["providers"].items())),
        "stand_in_forecast": F.LOCAL in d3["providers"],
        "lat_phrao_milk_recorded_day2": lp_day2,
        "sus19": sus19,
        "reviewer_lots": reviewer,
    }


def failures(report: dict) -> list[str]:
    """Why the CLI exits 1: each failing beat, Lat Phrao's day-2 milk when not 0, a reviewer first step not on day 4."""
    out = [f"beat {name} fails" for name in BEATS if not report["sus19"]["beats"][name]]
    if report["lat_phrao_milk_recorded_day2"] != 0:
        out.append(f"Lat Phrao recorded {report['lat_phrao_milk_recorded_day2']} milk on day {TODAY - 1}, not 0")
    out += [f"{lot_id} first step on day {v['first_step_day']}, not day {FIRST_STEP_DAY} (a D-18 defect)"
            for lot_id, v in report["reviewer_lots"].items() if v["first_step_day"] != FIRST_STEP_DAY]
    return out


def compare(old: dict, new: dict) -> list[str]:
    """One line per reviewer-case outcome that differs between two story reports: for every reviewer lot its
    recommended plan, kind, run, scenario wins and target met; SUS-19's plan after the count; each SUS-19 beat."""
    lines = []
    old_lots, new_lots = old.get("reviewer_lots", {}), new.get("reviewer_lots", {})
    for lot_id in sorted(set(old_lots) | set(new_lots)):
        a, b = old_lots.get(lot_id), new_lots.get(lot_id)
        if a is None or b is None:
            lines.append(f"{lot_id}: reviewer lot {'added' if a is None else 'removed'}")
            continue
        for field, get in (("recommended", lambda v: v["recommended"]), ("kind", lambda v: v["kind"]),
                           ("run_code", lambda v: v["run_code"]),
                           ("scenario_wins.wins", lambda v: v["scenario_wins"]["wins"]),
                           ("target_met", lambda v: v["target_met"])):
            if get(a) != get(b):
                lines.append(f"{lot_id} {field}: {get(a)!r} -> {get(b)!r}")
    sa, sb = old.get("sus19", {}), new.get("sus19", {})
    story = sb.get("lots", sa.get("lots", {})).get("case", "SUS-19")
    ra = sa.get("after_count", {}).get("recommended")
    rb = sb.get("after_count", {}).get("recommended")
    if ra != rb:
        lines.append(f"{story} after_count.recommended: {ra!r} -> {rb!r}")
    for name in BEATS:
        va, vb = sa.get("beats", {}).get(name), sb.get("beats", {}).get(name)
        if va != vb:
            lines.append(f"{story} beat {name}: {va!r} -> {vb!r}")
    return lines


def dumps(report: dict) -> str:
    """The text story.json is written as: sorted keys, one-space indent, UTF-8, a final newline."""
    return json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m data.story", description=__doc__.splitlines()[0])
    parser.add_argument("--ref", type=pathlib.Path, default=REF,
                        help=f"the reference set with seed.json, truth.json and forecast.json (default: {REF})")
    parser.add_argument("--out", type=pathlib.Path, default=None,
                        help="where to write the report (default: <ref>/reports/story.json)")
    parser.add_argument("--compare", type=pathlib.Path, default=None,
                        help="a saved story.json; every reviewer-case outcome that differs is listed (exit 3)")
    args = parser.parse_args(argv)
    out = args.out if args.out is not None else args.ref / "reports" / "story.json"
    report = run(args.ref)
    out.parent.mkdir(parents=True, exist_ok=True)
    config.atomic_write(out, dumps(report))
    s = report["sus19"]
    print(f"wrote {out}")
    print("beats: " + ", ".join(f"{name} {s['beats'][name]}" for name in BEATS))
    print(f"on the record: {s['on_record']['recommended_label']}; after the count: "
          f"{s['after_count']['recommended_label']}; {s['video_phrase']}")
    print("numbers: " + ", ".join(f"{k} {v} (about {s['about'][k]})" for k, v in s["numbers"].items()))
    print(f"demo line: {s['demo_line']}")
    print(f"Lat Phrao recorded milk on day {TODAY - 1}: {report['lat_phrao_milk_recorded_day2']}")
    for lot_id, v in report["reviewer_lots"].items():
        print(f"{lot_id} ({v['tag']}): {v['recommended_label']}; wins {v['scenario_wins']['wins']} of 3; first step "
              f"day {v['first_step_day']}; target {v['target']!r} met {v['target_met']}")
    failed = failures(report)
    for line in failed:
        print(f"FAIL {line}")
    if failed:
        return 1
    if args.compare is not None:
        old = json.loads(args.compare.read_text(encoding="utf-8"))
        changed = compare(old, report)
        for line in changed:
            print(f"CHANGED {line}")
        if changed:
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
