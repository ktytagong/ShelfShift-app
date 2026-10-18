"""Normalized attempt recorder, called only inside admission/finalization transactions."""
import math
import re

from api import db

METRICS = ("model_calls", "provider_attempts", "tool_calls", "retries", "input_tokens", "output_tokens",
           "thinking_tokens", "total_tokens", "tool_use_prompt_tokens", "first_text_ms", "total_ms")



def _start_attempt(conn, job):
    """Record exactly one admitted attempt in the claim/spend transaction."""
    ctx = job["ctx"]
    db.run(conn, "INSERT INTO ai_usage (run_id, attempt_id, output_id, request_id, session_id, case_id, "
                 "rec_version, plan_key, kind, model_id, location, precomputed, status) "
                 "VALUES (:r, :id, :id, :q, :s, :c, :v, :p, :k, :m, :loc, :pre, 'RUNNING')",
           r=ctx["run_id"], id=job["output_id"], q=job.get("request_id"), s=ctx.get("session_id"),
           c=ctx["case_id"], v=ctx["rec_version"], p=ctx["plan_key"], k=job["kind"],
           m=ctx["model_id"], loc=job["location"], pre=job["precomputed"])



def _finish_attempt(conn, job, stats, status, reason):
    """Unknown provider fields remain SQL NULL, including on interruption."""
    ctx = job["ctx"]
    durations = db.rows(conn, "SELECT result_id, tool, duration_ms FROM tool_results WHERE run_id = :r "
                             "AND request_id = :q AND case_id = :c AND scope = :sc "
                             "AND (:sc = 'CASE' OR session_id = :s) "
                             "AND rec_version IS NOT DISTINCT FROM :v AND plan_key IS NOT DISTINCT FROM :p "
                             "ORDER BY created_at, result_id",
                        r=ctx["run_id"], q=ctx["request_id"], c=ctx["case_id"], sc=ctx["scope"],
                        s=ctx.get("session_id"), v=ctx["rec_version"], p=ctx["plan_key"])
    fields = ", ".join(f"{key} = :{key}" for key in METRICS)
    db.run(conn, f"UPDATE ai_usage SET {fields}, tool_durations = CAST(:dur AS jsonb), status = :st, "
                 "reason = :why, finished_at = now() WHERE run_id = :r AND attempt_id = :id AND status = 'RUNNING'",
           **{key: stats.get(key) for key in METRICS}, dur=db.js(durations), st=status, why=reason,
           r=ctx["run_id"], id=job["output_id"])


def summary(attempts):
    """Report known sums and missing coverage without manufacturing provider usage.

    Timings and tokens remain measurements of attempts, never an invented price
    estimate. Each admitted retry/failure is included in the same retained rows.
    """
    result = {}
    for metric in METRICS:
        known = [row[metric] for row in attempts if row.get(metric) is not None]
        result[metric] = {"known_total": sum(known) if known else None,
                          "known_attempts": len(known), "unknown_attempts": len(attempts) - len(known)}
    return result



_STATUSES = {"RUNNING", "DONE", "FALLBACK", "INTERRUPTED"}
_REASONS = {"empty", "unavailable", "timeout", "tool_limit", "interrupted", "brief_too_long"}


def _number(value):
    return value if (isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value) and value >= 0) else None


def _label(value):
    # Configured model/region labels are public; qualified infrastructure names,
    # URLs and provider diagnostics are not. Never export them as labels.
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) else None


def for_output(conn, output):
    """Project retained metrics only after the caller authorizes the output.

    Recheck the saved tuple so a mismatched legacy row cannot attach another
    attempt's metrics. CASE attempts can be shared with every case viewer; ASK
    retains the original request and session. This function never generates.
    """
    row = db.one(conn, "SELECT * FROM ai_usage WHERE run_id = :r AND output_id = :o AND case_id = :c "
                      "AND rec_version = :v AND plan_key = :p AND kind = :k AND model_id = :m "
                      "AND (:k = 'BRIEF' OR (request_id = :q AND session_id = :s))",
                 r=output["run_id"], o=output["output_id"], c=output["case_id"], v=output["rec_version"],
                 p=output["plan_key"], k=output["kind"], m=output["model_id"],
                 q=output.get("request_id"), s=output.get("session_id"))
    if row is None:
        return None
    from . import tools
    names = {tool.__name__ for tool in tools.CASE_TOOLS}
    durations = [{"result_id": _label(item.get("result_id")),
                  "tool": item.get("tool") if item.get("tool") in names else "unknown",
                  "duration_ms": _number(item.get("duration_ms"))}
                 for item in row["tool_durations"] if isinstance(item, dict)]
    result = {key: _number(row.get(key)) for key in METRICS}
    result.update(model_id=_label(row["model_id"]), location=_label(row["location"]),
                  status=row["status"] if row["status"] in _STATUSES else "UNKNOWN",
                  reason=row["reason"] if row["reason"] in _REASONS or row["reason"] is None else "unavailable",
                  precomputed=row.get("precomputed"), tool_durations=durations)
    known_durations = sum(item["duration_ms"] is not None for item in durations)
    result["tool_durations_missing"] = (None if result["tool_calls"] is None else
                                        max(0, result["tool_calls"] - known_durations))
    result["missing"] = [key for key in (*METRICS, "model_id", "location", "precomputed") if result[key] is None]
    if result["tool_durations_missing"] is None or result["tool_durations_missing"]:
        result["missing"].append("tool_durations")
    return result


def distribution(values, *, samples):
    """Nearest-rank observed percentiles with explicit missing coverage."""
    known = sorted(value for value in values if _number(value) is not None)
    return {"samples": len(known), "missing": samples - len(known),
            "p50": known[math.ceil(len(known) * .50) - 1] if known else None,
            "p95": known[math.ceil(len(known) * .95) - 1] if known else None,
            "known_total": sum(known) if known else None}


def benchmark(attempts, *, measurement_source):
    """Aggregate one explicitly labelled collection of retained attempts.

    Fake/live provenance is supplied by the operator who selected the run;
    neither model labels nor PRECOMPUTED imply a live provider measurement.
    No per-person/output/source identity or generated/private text is returned.
    """
    if measurement_source not in {"local-fake", "user-observed-live"}:
        raise ValueError("measurement_source must be local-fake or user-observed-live")
    groups = {}
    for row in attempts:
        kind = row.get("kind") if row.get("kind") in {"BRIEF", "ANSWER"} else "UNKNOWN"
        precomputed = row.get("precomputed") if type(row.get("precomputed")) is bool else None
        groups.setdefault((kind, precomputed), []).append(row)
    report = []
    for (kind, precomputed), rows in sorted(groups.items(), key=lambda item: str(item[0])):
        statuses, reasons = {}, {}
        for row in rows:
            status = row.get("status") if row.get("status") in _STATUSES else "UNKNOWN"
            reason = row.get("reason") if row.get("reason") in _REASONS else "unavailable" if row.get("reason") else None
            statuses[status] = statuses.get(status, 0) + 1
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
        durations = [item.get("duration_ms") for row in rows for item in row.get("tool_durations", [])
                     if isinstance(item, dict)]
        missing_durations = sum(max(0, row["tool_calls"] - sum(
            _number(item.get("duration_ms")) is not None for item in row.get("tool_durations", [])
            if isinstance(item, dict))) for row in rows if _number(row.get("tool_calls")) is not None)
        report.append({"kind": kind, "precomputed": precomputed, "sample_count": len(rows),
                       "statuses": statuses, "reasons": reasons,
                       "metrics": {key: distribution([row.get(key) for row in rows], samples=len(rows)) for key in METRICS},
                       "tool_duration_ms": distribution(durations, samples=len(durations)),
                       "tool_durations_missing": missing_durations,
                       "tool_durations_unknown_attempts": sum(_number(row.get("tool_calls")) is None for row in rows)})
    return {"measurement_source": measurement_source, "percentile_method": "nearest-rank",
            "sample_count": sum(len(rows) for rows in groups.values()), "groups": report}


def configured_limits():
    """Read public request/session settings; never include global counters/IDs."""
    from . import config
    settings = config.load()
    limit, window = config.api_config.SESSION_CAPS["ai"]
    return {key: getattr(settings, key) for key in ("max_output_tokens", "max_request_tokens", "max_tool_calls",
                                                  "max_llm_calls", "seconds")} | {
        "thinking_level": "LOW", "session_ai_requests": {"limit": limit, "window": window}}


def main(argv=None):
    """Read one operator-selected run; no model, tools, cap spend or mutation."""
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(prog="python -m agent.usage", description="Aggregate retained assistant metrics only.")
    parser.add_argument("run_id", metavar="RUN_ID")
    parser.add_argument("--measurement-source", required=True, choices=("local-fake", "user-observed-live"),
                        help="explicit origin of the selected records; never inferred from model/provenance")
    parser.add_argument("--output", type=Path, help="write metadata-only JSON instead of stdout")
    args = parser.parse_args(argv)
    with db.tx() as conn:
        attempts = db.rows(conn, "SELECT kind, precomputed, status, reason, tool_durations, " + ", ".join(METRICS) +
                          " FROM ai_usage WHERE run_id = :r", r=args.run_id)
    report = benchmark(attempts, measurement_source=args.measurement_source)
    report["limits"] = configured_limits()
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
