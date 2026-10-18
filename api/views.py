"""Read-only views over one sandbox: the session JSON and a case's activity (04-API-CONTRACT "Shapes", GET
/api/session and GET /api/cases/{id}/activity; spec §10).

The session JSON is the client's frame for everything else (04-CONTEXT D-16): the signed-in account and the seat it
derives (06.1 D-47; the seat list went with the seat switch), the sandbox's day and run version, the clock and the
calendar that name each day's weekday from the dataset's own calendar (`Ref.date`; a public set's is its shifted
calendar, 05.2 D-13; never "day N", Carried IN-02), whether the demo is complete, the last reset time, the open count
requests addressed to the seat, demo mode, the reference currency, the dataset's `kind` (synthetic or public), its
`data_as_of` scenario date (ISO, null for the synthetic set) and its label (the synthetic set's "Synthetic data ·
hypothetical scenario"; a public set's mark template, whose {date} the UI fills with the formatted `data_as_of`, 05.2
D-04), the request settings the case page states (`requests`: `response_min`, `max_per_version`; 07-21), and the
shell's `nav_counts` for the signed-in account (api/planning.py). Nothing here reads physical stock or
hidden demand, and nothing calls the engine (OPS-06).

The activity view lists a case's `activity_log` entries by recorded time (`at`, then the log sequence), each in one
of the contract's groups (taking ownership is a decision, 06.1-08), with its `actor` (the account that acted, or
null for the screen and the engine) and its `scenario_date` (the ISO date of its day from the dataset's own
calendar), 06.1 D-19, D-58. Phase 9 adds the assistant's tool calls.

The export (04-API-CONTRACT "Export"; spec §6 Export) is the engine's `export_bundle` of the active version's stored
raw input, its stored result with the engine's wait text kept (the only response that carries it, 03-CONTEXT D-24),
its fact checks, the dataset and forecast run ids, the case key and the dataset's label (a public set's with its
`{date}` filled from the scenario date, `cases.mark_label`). The stored input is checked
by the engine first, so a bundle never labels another engine version's input with this one's (round-2 IN-04):
another engine version is 409 engine_version. Replaying the bundle's input offline reproduces its rec_version and
result. Nothing here calls the engine worker.
"""
from __future__ import annotations

from datetime import timezone

import sqlalchemy as sa

from engine.adjust import InputError, validate_input
from engine.canonical import export_bundle, from_dict
from engine.model import ENGINE_VERSION
from engine.timing import fmt_min

from . import accounts, cases, config, db, planning, refdata, signals
from .errors import DB_BUSY, ApiError, engine_error

DECISIONS = ("ownership_taken", "sent", "decision", "committed", "prepared", "waiting", "rejected",
             "assumptions_adopted", "approval_cancelled", "hold_expired", "invalidated", "executed", "observed",
             "closed", "second_look")

def calendar(ref: refdata.Ref) -> list[dict]:
    """Every day from 0 through the horizon end, with its ISO date and weekday."""
    return [{"day": d, "date": ref.date(d).isoformat(), "weekday": cases.weekday(d, ref)}
            for d in range(0, ref.calendar["horizon_end"] + 1)]


def clock(ref: refdata.Ref, day: int) -> str:
    """The demo day and its opening time with the weekday ("Thu 15 Oct, 09:00"); "Demo complete" after the last
    demo day."""
    if day > ref.calendar["last_demo_day"]:
        return "Demo complete"
    return f"{cases.date_label(day, ref)}, {fmt_min(ref.calendar['open_min'])}"


def session_info(conn, session: dict, run: dict | None = None) -> dict:
    """The session JSON (04-API-CONTRACT Session): every key present, the seat from the session row, the rest from
    its sandbox and the reference data."""
    run = run or db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=session["run_id"])
    ref = refdata.get(run["dataset_version"])
    requests = 0
    if session["role"] == "manager":
        requests = db.one(conn, "SELECT count(*) AS n FROM evidence_requests WHERE run_id = :r AND to_branch = :b "
                                "AND state = 'OPEN'", r=run["run_id"], b=session["branch"])["n"]
    return {
        "account": session.get("account"), "role": session["role"], "branch": session["branch"],
        "run_id": run["run_id"], "day": run["day"], "version": run["version"],
        "clock": clock(ref, run["day"]), "calendar": calendar(ref),
        "demo_complete": run["day"] > ref.calendar["last_demo_day"],
        "last_reset_ms": session["last_reset_ms"], "open_requests": int(requests),
        "tasks_waiting": signals.waiting(conn, run, session),  # GET /api/tasks counts' sum for the seat (07 D-04)
        "demo_mode": config.DEMO_MODE, "currency": ref.currency, "label": ref.label,
        "kind": ref.kind, "data_as_of": ref.data_as_of,  # the mark's scenario date, formatted by the UI (05.2 D-04)
        "time_zone": config.CAP_TZ.key,  # recorded times are shown in this zone (spec rev 6.7 §5; caps.time_zone)
        # the request panel's deadline and the per-version limit, from settings (07-CONTEXT D-05, D-07; 07-21)
        "requests": {"response_min": config.REQUEST_RESPONSE_MIN, "max_per_version": config.MAX_REQUESTS_PER_VERSION},
        "nav_counts": planning.nav_counts(conn, run, session),  # Today's cards, open decisions, tasks (06.1-13)
    }


def _group(type_: str) -> str:
    """The Activity tab's group for a log entry type (04-API-CONTRACT GET /api/cases/{id}/activity)."""
    if type_ in DECISIONS:
        return "Decisions"
    if type_ == "evidence_drafted":
        return "Assistant"
    return "Data"


PEOPLE = ("manager", "hq")  # the roles that name a person in a log payload (a decision's "source"/"dest" do not)


def actor(payload: dict) -> dict | None:
    """Who acted, as {account, role, branch}: the payload's own `actor` when it has one (06.1 writes), else its
    `role` (or settings' `by`) and `branch` when they name a person (the store's manager, or HQ), else None (the
    screen and the engine)."""
    if isinstance(payload.get("actor"), dict):
        a = payload["actor"]
        return {"account": a.get("account"), "role": a.get("role"), "branch": a.get("branch")}
    role = payload.get("role", payload.get("by"))
    if role == "hq":
        return {"account": accounts.HQ, "role": "hq", "branch": None}
    if role == "manager" and isinstance(payload.get("branch"), str):
        return {"account": payload["branch"], "role": "manager", "branch": payload["branch"]}
    return None


def _private_activity(payload) -> bool:
    """Reject private records even when an old producer nested its metadata."""
    if isinstance(payload, dict):
        if payload.get("scope") == "ASK" or payload.get("kind") == "ANSWER" or any(
                key in payload for key in ("question", "answer_text", "tool_results", "tool_result", "chat_history",
                                          "chat_messages", "personal_history", "thought", "thoughts",
                                          "thought_signature", "credentials")):
            return True
        # A legacy producer may have omitted scope/kind on an embedded audit.
        # Its existence never authorizes raw source detail in a shared event.
        if "result_id" in payload and ("result" in payload or "inputs" in payload):
            return True
        return any(_private_activity(value) for value in payload.values())
    if isinstance(payload, list):
        return any(_private_activity(value) for value in payload)
    return False


def activity(conn, run: dict, case_id: str) -> list[dict]:
    """The case's activity log by recorded time, then log sequence (so a count reads before the version it caused
    in the same transaction), each with its actor and scenario date; 404 for a case not in this sandbox."""
    cases.case_row(conn, run["run_id"], case_id)
    ref = refdata.get(run["dataset_version"])
    drafts = {row["draft_id"]: row for row in db.rows(conn,
        "SELECT draft_id, rec_version, plan_key, fact_id, class, owner, to_branch, sku, lot_ids, model_id, "
        "assistant_version FROM evidence_drafts WHERE run_id = :r AND case_id = :c", r=run["run_id"], c=case_id)}
    result = []
    for row in db.rows(conn, "SELECT * FROM activity_log WHERE run_id = :r AND case_id = :c ORDER BY at, seq",
                       r=run["run_id"], c=case_id):
        payload = row["payload"]
        # Preserve the open business-event/actor contract while excluding
        # personal conversation and raw tool records, including nested ASK data.
        if row["type"].split("_", 1)[0] in {"ask", "chat", "answer", "question", "tool"} or _private_activity(payload):
            continue
        if row["type"] == "evidence_drafted":
            # Resolve only this run/case's persisted draft, then project safe
            # provenance. Original prose/context belongs to authorized detail.
            payload = drafts.get(payload.get("draft_id"))
            if payload is None:
                continue
        result.append({"at": row["at"].astimezone(timezone.utc).isoformat(), "day": row["day"], "type": row["type"],
                       "group": _group(row["type"]), "payload": payload, "actor": actor(payload),
                       "scenario_date": ref.date(row["day"]).isoformat()})
    return result


def export(conn, run: dict, case_id: str) -> dict:
    """The active version as one JSON file: the stored raw input, the stored result (wait text kept), its fact
    checks and the versions. 404 for a case not in this sandbox; 409 missing_data without a recommendation; 409
    engine_version when the stored input is another engine version's."""
    case = cases.case_row(conn, run["run_id"], case_id)
    rec = cases.active_rec(conn, case)
    if rec is None:
        raise ApiError(409, "missing_data", "This case has no recommendation")
    stored = rec["engine_input"]
    foreign = ApiError(409, "engine_version", "This case was computed by another engine version",
                       engine_version=stored.get("engine_version"), current=ENGINE_VERSION)
    try:
        inp = from_dict(stored)
    except (KeyError, TypeError):  # another engine version's shape
        raise foreign from None
    try:
        validate_input(inp)
    except InputError as exc:
        if inp.engine_version != ENGINE_VERSION:
            raise foreign from None
        raise engine_error(exc) from None
    ref = refdata.get(run["dataset_version"])
    return export_bundle(inp, rec["result"], fact_checks=rec["fact_checks"], dataset_version=run["dataset_version"],
                         forecast_run_id=ref.forecast_runs[rec["day"]]["forecast_run_id"], case=case["key"],
                         label=cases.mark_label(ref))  # {date} filled: no raw placeholder in a file (05.2 D-04)


def series(conn, run: dict, store: str, sku: str, from_day: int, to_day: int) -> dict:
    """GET /api/series/{store}/{sku}?from=&to= (05.2 D-11): one series' history and forecast for a window of day
    indexes, never the whole dataset.

    `history`: the set's daily rows (`Ref.history`: units, stockout, discount, unknown price, holiday, activity,
    weather) for the window's days before the workspace's day, each with its date from the dataset calendar.
    `forecast`: the forecast made on the workspace's day for the window's days from that day on, each point with its
    provider. 404 not_found for a store or product the set does not have; 400 bad_request when `from` is after `to`
    or the window is longer than the set's `series.max_days`. Hidden demand and physical stock are never read: the
    held-out days (the workspace's day and later) come only as forecasts. A history read that runs past the set's
    statement timeout is 503 db_busy."""
    ref = refdata.get(run["dataset_version"])
    if store not in ref.branches or sku not in ref.products:
        raise ApiError(404, "not_found", f"No series {store} {sku}")
    if from_day > to_day:
        raise ApiError(400, "bad_request", "from must not be after to")
    longest = int(ref.series["max_days"])
    if to_day - from_day + 1 > longest:
        raise ApiError(400, "bad_request", f"A window holds at most {longest} days")
    day = run["day"]
    try:
        history = ref.history(store, sku, from_day, min(to_day, day - 1)) if from_day < day else []
    except sa.exc.DBAPIError as exc:
        if "statement timeout" not in str(exc):
            raise
        raise ApiError(503, "db_busy", DB_BUSY) from None
    points = ref.forecast(day, store, sku)
    forecast = [{"day": d, "date": ref.date(d).isoformat(), "low": lo, "base": base, "high": hi,
                 "provider": ref.provider_of(day, store, sku, d)}
                for d, (lo, base, hi) in sorted(points.items()) if max(from_day, day) <= d <= to_day]
    return {"store": store, "sku": sku, "from": from_day, "to": to_day, "day": day,
            "history": [{**h, "date": ref.date(h["day"]).isoformat()} for h in history], "forecast": forecast}
