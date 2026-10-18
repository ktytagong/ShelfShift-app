"""Explicit release baseline job: python -m agent.briefs RUN_ID.

Paid production execution is user-run only, after an inactive template build.
Every candidate uses jobs.prepare_brief/run_job: the normal scoped tools,
checked drafts, final-key recheck and retained failure/usage path. Importing this
module performs no work. Build, activation, startup and reads never invoke it.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import aclosing
import json
import sys

from api import db
from api.errors import ApiError
from data.config import ConfigError
from . import config, jobs, usage


def _report(**row):
    print(json.dumps(row, ensure_ascii=False, sort_keys=True), flush=True)


def _cases(run_id):
    with db.tx() as conn:
        run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r FOR SHARE", r=run_id)
        if run is None or run["kind"] != "TEMPLATE" or run["status"] != "READY" or run["failure"]:
            raise ValueError("The baseline job requires a completed inactive template (READY).")
        return db.rows(conn, "SELECT case_id, active_rec_version, missing_data FROM cases "
                            "WHERE run_id = :r ORDER BY case_id", r=run_id)


async def run(run_id):
    settings = config.load()
    cases = _cases(run_id)  # enumerate every case, including unevaluated flags and warm-up cases
    counts = dict(cases=len(cases), eligible=0, ineligible=0, attempted=0, success=0, failure=0, reused=0)
    attempt_ids = set()
    for case in cases:
        cid = case["case_id"]
        if case["active_rec_version"] is None:
            counts["ineligible"] += 1
            _report(type="case", run_id=run_id, case_id=cid, status="ineligible",
                    reason=case["missing_data"] or "No evaluated recommendation.",
                    first_open="Live after evaluation creates a recommendation; normal AI admission applies.")
            continue
        counts["eligible"] += 1
        job = jobs.prepare_brief(run_id, cid, baseline=True)
        final = None
        async with aclosing(jobs.run_job(job)) as stream:
            async for event in stream:
                if event["type"] == "brief":
                    final = event
        if "saved" in job:
            counts["reused"] += 1
            outcome = "reused"
        else:
            if "output_id" in job:
                counts["attempted"] += 1
                attempt_ids.add(job["output_id"])
            ok = final is not None and final["status"] == "CURRENT" and final["provenance"] == "PRECOMPUTED"
            outcome = "success" if ok else "failure"
            counts[outcome] += 1
        _report(type="case", run_id=run_id, case_id=cid, rec_version=job["ctx"]["rec_version"],
                plan_key=job["ctx"]["plan_key"], status=outcome,
                output_id=final["output_id"] if final else None,
                label=final["label"] if final else jobs.GENERATING,
                reason=final.get("reason") if final else "Another baseline generation is in progress.",
                first_open="Live first-open exception; normal AI admission applies." if outcome == "failure" else None)
    with db.tx() as conn:
        attempts = db.rows(conn, "SELECT * FROM ai_usage WHERE run_id = :r AND kind = 'BRIEF' "
                                "AND precomputed IS TRUE ORDER BY started_at, attempt_id", r=run_id)
    _report(type="summary", run_id=run_id, **counts,
            model_id=settings.model_id, location=settings.location, assistant_version=config.ASSISTANT_VERSION,
            usage=usage.summary([row for row in attempts if row["attempt_id"] in attempt_ids]),
            baseline_attempts=len(attempts), baseline_usage=usage.summary(attempts),
            cost=None, cost_reason="Billed cost is not recorded here; retain provider usage for the release record.")
    # Provider failures are release-nonfatal. They remain Fallback, never successful Precomputed rows.
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m agent.briefs", description=__doc__.splitlines()[0])
    parser.add_argument("run_id", metavar="RUN_ID", help="the completed inactive template to prepare")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args.run_id))
    except (ValueError, ConfigError, ApiError) as exc:
        print(f"baseline job refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
