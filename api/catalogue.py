"""The reviewer cases of a public template, picked by a stated rule (spec §5 Public dataset "Reviewer cases on the
public dataset", §8 Template build step 4; 05.2-CONTEXT D-18, D-25, D-26, D-38).

    python -m api.catalogue report [--template RUN] --out PATH   # the catalogue report of a template (default ACTIVE)

A rule stated before the data is generated; what the data gives is reported, never forced. On a public template's
open day the morning screen has flagged every qualifying lot ("Needs evaluation", api/flags.py). `pick` evaluates
them in the stated order (the screen's stock at the step day, then Keep waste, both descending, a flag the screen
could not judge last, then key by codepoint), each through `flags.open_from_flag` (the engine, its fact checks and the
shared cache, exactly as a visitor's open), until every kind of the reference `catalogue.kinds` has a tagged case or
the budget `catalogue.budget` is spent. No other lot gets a search (D-25): the rest stay flags, visible and counted.

The kinds, in the reference's priority order, each a predicate over what the case detail holds (the stored result,
its fact checks, the stored raw input and the stage-1 eligibility):

- verify (verify-before-move): a PHANTOM_STOCK or STALE_COUNT fact with pick_changed true and decision_regret at or
  above the input Policy's min_regret (a count would change the plan);
- transfer, markdown, keep: the recommended plan's kind;
- cold_chain (cold-chain-excluded): a run excluded or rejected for "cold chain" (stage 1 drops it on a public set);
- transfers_unavailable (transfers-unavailable): the source store is missing from the location mapping (D-31);
- close_call (close-call): the recommended plan wins 2 of the 3 demand scenarios.

An evaluated item may match several kinds; it is tagged with the first kind in that order that has no case yet, so no
two kinds tag the same case and each tag marks the first evaluated item of its kind that an earlier kind did not take.
The tag is the kind's reference name (the case's `tag` column, cloned into every workspace), so the judge's path, the
regression set and the tests find the cases by tag. A kind with no tagged case is reported missing; it never fails the
build (05.2-18 is the gate, D-38). The run-level `catalogue` activity entry logs the budget, the flag count and every
evaluated key in order with the kinds it matched, so the report never re-derives what ran.

Nothing here tunes a seed value, the error rule or the order to make a kind appear (benchmarks, not targets). The
budget, order text, kinds, required kinds, start kinds and warm-up scope are reference settings, never code constants;
the definitions above are the rule and live here.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from . import cases, db, eligibility, flags, overlay, refdata
from .errors import ApiError

VERIFY_FACTS = ("PHANTOM_STOCK", "STALE_COUNT")
COLD_CHAIN = "cold chain"  # the engine's and stage 1's wording (engine.limits, api/eligibility.py)
UNMAPPED = "unavailable_unmapped"  # eligibility.summary's status for a store the mapping lacks (D-31)
LOG = "catalogue"  # the run-level activity entry `pick` writes


def _plan(facts: dict) -> str | None:
    return (facts.get("result") or {}).get("recommended")


def _verify(facts: dict) -> bool:
    if not facts.get("result") or not facts.get("engine_input"):
        return False
    floor = facts["engine_input"]["policy"]["min_regret"]
    return any(c["kind"] in VERIFY_FACTS and c["pick_changed"] and c.get("decision_regret") is not None
               and c["decision_regret"] >= floor for c in facts.get("fact_checks") or [])


def _cold_chain(facts: dict) -> bool:
    result, elig = facts.get("result"), facts.get("eligibility") or {}
    if not result:
        return False
    return any(COLD_CHAIN in r["reasons"] for r in result["rejected"]) or \
        any(x["reason"] == COLD_CHAIN for x in elig.get("excluded") or [])


def _close_call(facts: dict) -> bool:
    return bool(facts.get("result")) and facts["result"]["scenario_wins"]["wins"] == 2


# tag -> (report name, predicate). The reference `catalogue.kinds` names these tags in priority order.
KINDS = {
    "verify": ("verify-before-move", _verify),
    "transfer": ("transfer", lambda f: (_plan(f) or "").startswith("tr:")),
    "markdown": ("markdown", lambda f: (_plan(f) or "").startswith("md")),
    "keep": ("keep", lambda f: _plan(f) == "keep"),
    "cold_chain": ("cold-chain-excluded", _cold_chain),
    "transfers_unavailable": ("transfers-unavailable", lambda f: bool(f.get("result"))
                              and (f.get("eligibility") or {}).get("status") == UNMAPPED),
    "close_call": ("close-call", _close_call),
}
RULE = ("Flagged lots of the open day, in the stated order, each evaluated and fact-checked through the engine until "
        "every kind has a tagged case or the budget is spent; an item is tagged with the first kind in the reference "
        "order that it matches and that has no case yet. verify-before-move: a PHANTOM_STOCK or STALE_COUNT check "
        "with pick_changed true and decision_regret >= the input Policy's min_regret; transfer / markdown / keep: the "
        "recommended plan's kind; cold-chain-excluded: a run excluded or rejected for 'cold chain'; "
        "transfers-unavailable: the source store is missing from the location mapping; close-call: the recommended "
        "plan wins 2 of 3 scenarios. Kinds that do not occur are reported, never forced.")


def rule(ref) -> dict:
    """The set's catalogue settings (reference `catalogue`: budget, kinds, order, required, start_kind,
    warmup_scope). Every kind it names must have a definition here; a warm-up scope other than "none" is refused,
    since only D-38's "none" is built."""
    cat = ref.settings["catalogue"]
    unknown = [k for k in cat["kinds"] + cat["required"] + cat["start_kind"] if k not in KINDS]
    if unknown:
        raise ValueError(f"catalogue names kinds with no definition: {', '.join(unknown)}")
    if cat["warmup_scope"] != "none":
        raise ValueError(f"catalogue warm-up scope {cat['warmup_scope']!r} is not built (D-38 chose none)")
    return cat


def applies(ref) -> bool:
    """Whether this set's reviewer cases come from the catalogue (a public set that carries the settings)."""
    return ref.kind == "public" and "catalogue" in ref.settings


def kind_of(facts: dict) -> list[str]:
    """Every kind (tag) an evaluated item matches, in KINDS order. `facts` holds what the case detail holds:
    result, fact_checks, engine_input and eligibility (an item with no result, missing data, matches none)."""
    return [tag for tag, (_, test) in KINDS.items() if test(facts)]


def _ordered_flags(conn, run: dict, limit: int | None = None) -> list[dict]:
    """The run's flags of its day in the stated order (D-38)."""
    sql = ("SELECT * FROM case_flags WHERE run_id = :f AND day = :d ORDER BY stock_at_step DESC NULLS LAST, "
           "keep_waste DESC NULLS LAST, key COLLATE \"C\"")
    return db.rows(conn, sql + (" LIMIT :n" if limit is not None else ""), f=overlay.flags_run(conn, run),
                   d=run["day"], **({"n": limit} if limit is not None else {}))


def pick(conn, run: dict, settings: dict | None = None) -> dict:
    """Evaluate the run's flagged lots in the stated order and tag the first item of each kind (see the module doc);
    log and return {budget, flagged, items: [{key, kinds}], tags: {tag: key}}. Inside the caller's transaction (the
    template build's check step on the open day)."""
    ref = refdata.get(run["dataset_version"])
    cat = settings or rule(ref)
    order, budget = list(cat["kinds"]), int(cat["budget"])
    flagged = db.one(conn, "SELECT count(*) AS n FROM case_flags WHERE run_id = :f AND day = :d",
                     f=overlay.flags_run(conn, run), d=run["day"])["n"]
    items, tags = [], {}
    for flag in _ordered_flags(conn, run, budget):
        if len(tags) == len(order):
            break
        try:
            detail = flags.open_from_flag(conn, run, flag["key"])
        except ApiError as exc:  # the lot is gone: nothing was opened (counted, never retried)
            items.append({"key": flag["key"], "kinds": [], "skipped": exc.code})
            continue
        kinds = kind_of(detail)
        items.append({"key": flag["key"], "kinds": kinds})
        free = next((t for t in order if t in kinds and t not in tags), None)
        if free is not None:
            tags[free] = flag["key"]
            db.run(conn, "UPDATE cases SET tag = :t WHERE run_id = :r AND case_id = :c", t=free, r=run["run_id"],
                   c=flag["key"])
    out = {"budget": budget, "flagged": int(flagged), "items": items, "tags": tags}
    db.log(conn, run["run_id"], run["day"], LOG, None, **out)
    return out


def _logged(conn, run: dict) -> dict | None:
    row = db.one(conn, "SELECT payload FROM activity_log WHERE run_id = :r AND day = :d AND type = :t "
                       "ORDER BY seq DESC LIMIT 1", r=run["run_id"], d=run["day"], t=LOG)
    return row["payload"] if row else None


def problems(conn, run: dict) -> list[str]:
    """The public template's own checks, each failure named (empty = all good): the catalogue ran on the run's day,
    it stayed within its budget, and every tagged case is live with a recommendation, opened on that day, the only
    case with its tag. A missing kind is not a problem here (05.2-18 is the gate)."""
    ref = refdata.get(run["dataset_version"])
    cat, log = rule(ref), _logged(conn, run)
    if log is None:
        return [f"no catalogue ran on day {run['day']}"]
    out = []
    if len(log["items"]) > int(cat["budget"]):
        out.append(f"catalogue evaluated {len(log['items'])} items, over its budget of {cat['budget']}")
    for tag, key in log["tags"].items():
        found = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND tag = :t", r=run["run_id"], t=tag)
        if [c["case_id"] for c in found] != [key]:
            out.append(f"{tag}: expected only {key} tagged, found {', '.join(c['case_id'] for c in found) or 'none'}")
            continue
        case = found[0]
        if case["state"] not in cases.LIVE_STATES or case["opened_day"] != run["day"]:
            out.append(f"{tag}: {key} is {case['state']} (opened day {case['opened_day']}), not live from day "
                       f"{run['day']}")
        if not case["active_rec_version"]:
            out.append(f"{tag}: {key} has no recommendation ({case['missing_data']})")
    return out


def start_seat(conn, template: dict) -> str | None:
    """The store a new session on this template starts at (D-26, D-38): the source store of the case tagged with the
    first reference `start_kind` that has one (verify, then transfer); None on a set with no catalogue or no such
    case (the caller then takes the first store)."""
    ref = refdata.get(template["dataset_version"])
    if not applies(ref):
        return None
    for tag in rule(ref)["start_kind"]:
        row = db.one(conn, "SELECT source_branch FROM cases WHERE run_id = :r AND tag = :t ORDER BY case_id LIMIT 1",
                     r=template["run_id"], t=tag)
        if row:
            return row["source_branch"]
    return None


def _deciding(tag: str, facts: dict) -> dict:
    """What decides the kind for this case: the fact, the excluded runs, the status or the scenario wins."""
    result = facts["result"]
    if tag == "verify":
        floor = facts["engine_input"]["policy"]["min_regret"]
        c = next(c for c in facts["fact_checks"] if c["kind"] in VERIFY_FACTS and c["pick_changed"]
                 and c.get("decision_regret") is not None and c["decision_regret"] >= floor)
        return {k: c.get(k) for k in ("fact_id", "kind", "branch", "lot_ids", "recorded_value", "alt_value",
                                       "decision_regret", "best_plan_if_alt")} | {"min_regret": floor}
    if tag == "cold_chain":
        return {"excluded": [x for x in facts["eligibility"]["excluded"] if x["reason"] == COLD_CHAIN],
                "rejected": [r["run_code"] for r in result["rejected"] if COLD_CHAIN in r["reasons"]]}
    if tag == "transfers_unavailable":
        return {"eligibility": facts["eligibility"]["status"]}
    if tag == "close_call":
        return {"scenario_wins": result["scenario_wins"]}
    return {"recommended": result["recommended"]}


def _facts(conn, run: dict, case: dict) -> dict:
    rec = cases.active_rec(conn, case)
    return {"result": rec["result"] if rec else None, "fact_checks": rec["fact_checks"] if rec else [],
            "engine_input": rec["engine_input"] if rec else None,
            "eligibility": eligibility.summary(conn, run, case)}


def report(conn, run: dict) -> dict:
    """The catalogue report of a template (codes and plan keys only): the rule and order, the budget, the flags
    against those evaluated (D-38: evaluated versus total), the evaluation order, every kind with its count among the
    evaluated items and its tagged case (key, store code, product code, lot, recommended plan, the deciding fact),
    the missing kinds and the start seat."""
    ref = refdata.get(run["dataset_version"])
    cat, log = rule(ref), _logged(conn, run)
    log = log or {"budget": int(cat["budget"]), "flagged": None, "items": [], "tags": {}}
    counts = {tag: sum(1 for i in log["items"] if tag in i["kinds"]) for tag in KINDS}
    kinds = {}
    for tag in cat["kinds"]:
        name, entry = KINDS[tag][0], None
        key = log["tags"].get(tag)
        if key is not None:
            case = cases.case_row(conn, run["run_id"], key)
            facts = _facts(conn, run, case)
            entry = {"key": key, "store": case["source_branch"], "product": case["sku"], "lot": case["lot_id"],
                     "state": case["state"], "recommended": facts["result"]["recommended"],
                     "recommended_label": facts["result"]["recommended_label"], "deciding": _deciding(tag, facts)}
        kinds[name] = {"tag": tag, "count": counts[tag], "case": entry}
    return {"rule": RULE, "order_rule": cat["order"], "dataset_version": run["dataset_version"],
            "template": run["run_id"], "day": run["day"], "budget": log["budget"], "flagged": log["flagged"],
            "evaluated": len(log["items"]), "order": [i["key"] for i in log["items"]],
            "items": log["items"], "kinds": kinds,
            "required": [KINDS[t][0] for t in cat["required"]],
            "missing": [KINDS[t][0] for t in cat["kinds"] if t not in log["tags"]],
            "start_seat": start_seat(conn, run) or ref.branches[0]}


def main(argv: list[str] | None = None) -> int:
    from . import sandbox  # sandbox builds the template and calls pick and problems
    ap = argparse.ArgumentParser(prog="python -m api.catalogue", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    rp = sub.add_parser("report", help="write a template's catalogue report as JSON")
    rp.add_argument("--template", help="the template run id (default: the ACTIVE template)")
    rp.add_argument("--out", required=True, help="where to write the report")
    args = ap.parse_args(argv)
    with db.tx() as conn:
        run = (db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r AND kind = 'TEMPLATE'", r=args.template)
               if args.template else sandbox.active_template(conn))
        if run is None:
            print("no such template: build one with python -m api.sandbox build", file=sys.stderr)
            return 2
        if not applies(refdata.get(run["dataset_version"])):
            print(f"{run['run_id']} is not a public template: it has no catalogue", file=sys.stderr)
            return 2
        rep = report(conn, run)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")
    print(f"catalogue of {run['run_id']}: {rep['evaluated']} of {rep['flagged']} flags evaluated; missing: "
          f"{', '.join(rep['missing']) or 'none'}; start seat {rep['start_seat']}; wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
