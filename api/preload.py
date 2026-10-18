"""Preloaded simulated requests, responses and reports in every workspace (SIG-05; 07-CONTEXT D-05, D-18, D-20, D-39).

By a stated rule; never padded; spends nothing.

**The rule** (`generate`): the template build runs it once, in `sandbox.build_template`'s check step, after the warm-up
(and after 05.2's catalogue on a public set) and before the checks. It reads only the template's own cases:

- the live cases (OPEN, WAITING, PENDING_APPROVAL) in case-number order, skipping the judge's cases (`JUDGE_TAGS`: the
  story case, and a public set's verify-before-move case), so the reviewer makes the first request there;
- the candidates: each such case's PHANTOM_STOCK and STALE_COUNT facts that change the plan in force
  (`commitment.checks_in_force`), in the engine's order;
- the candidates take the `ROLES` in order: the first gets a request left unanswered; the next a request answered
  with a photo only (the packaged file of the samples' photo-only answer, `asset://<name>`, D-19), checked "now", so
  it reads "Photo received · count missing". Any further candidate gets nothing;
- the report anchor: the first candidate's case when it has a transfer option on today's run, else the first live case
  in case-number order that is not the judge's and has one (D-39). Its first such option, in the engine's rank order,
  names a destination and a run: a CAPACITY report by the destination's manager (the case product's zone, half the
  free totes of today's capacity row, valid today) and a TRANSPORT quote by HQ for the run (the run's own fixed cost
  from the data, per trip, handling included, valid today), both left unconfirmed (READ).

Each item goes through the service a person's goes through (`signals.request_evidence`, `signals.answer`,
`reports.create`), as the branch's own manager or HQ (system actors, as the warm-up's autopilot), and is then marked
provenance 'simulated': its views read "Simulated request", "Simulated response" or "Simulated report" (D-18). A
request or a report spends nothing, and the photo is a packaged file read where it is, so no cap is ever spent (no
`caps` call here). `demo_runs.preloaded_at` records the transaction's now().

**Minimums** (`minimums`): one unanswered request, one photo-only response, and one unconfirmed report for each owning
role (a store manager's branch fact, HQ's transport quote). What the data does not give is named in the run-level
`preload` activity entry, which `python -m api.sandbox build` prints; it never fails the build and nothing is invented
to meet it (D-39, as 05.2's reviewer-case rule; the release check reports it in the release record).

**Clone** (`rebase`, at the end of `sandbox.clone`): each workspace's preloaded times start when it is created.
Nothing is generated, answered or filed after a workspace is created.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

from engine.model import TRANSFER

from . import cases, commitment, config, db, files, realtime, refdata, reports, signals

JUDGE_TAGS = (cases.STORY_TAG, "verify")  # the story case; "verify" is api/catalogue.py's verify-before-move tag
ROLES = ("unanswered", "photo_only")  # what the candidates get, in order (D-18's minimums)
SIMULATED = signals.SIMULATED
LOG = "preload"  # the run-level activity entry `generate` writes
FILED = ("evidence_requested", "evidence_answered", "report_filed")  # the entries `generate` writes for each item
SESSION = "preload"  # the system actors' session id (the warm-up's is "warmup")
PHOTO_ONLY = "photo-only"  # the samples entry whose file the preloaded photo-only answer carries (07-02)
MISSING = {"unanswered": "no preloaded unanswered request", "photo_only": "no preloaded photo-only response",
           "manager": "no preloaded unconfirmed report for a store manager",
           "hq": "no preloaded unconfirmed report for HQ"}
RULE = ("Live cases in case-number order, the judge's cases skipped; their stock-count facts that change the plan in "
        "force, in the engine's order: the first gets an unanswered request, the next a photo-only answer. The first "
        "such case with a transfer option on today's run, else the first live case that has one, gets a CAPACITY "
        "report at the option's destination and a TRANSPORT quote for its run. Minimums the data does not give are "
        "reported, never padded.")


def _manager(branch: str) -> dict:
    return {"account": branch, "role": "manager", "branch": branch, "session_id": SESSION}


HQ = {"account": "HQ", "role": "hq", "branch": None, "session_id": SESSION}


def live_cases(conn, run: dict) -> list[dict]:
    """The run's live cases in case-number order (the key's number: SUS-9 before SUS-10), the judge's skipped."""
    rows = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND state = ANY(:s) ORDER BY length(case_id), case_id",
                   r=run["run_id"], s=list(cases.LIVE_STATES))
    return [c for c in rows if c["tag"] not in JUDGE_TAGS]


def candidates(conn, run: dict) -> list[tuple[dict, dict, dict]]:
    """(case, active version, fact) for every count fact that changes the plan in force, in the rule's order."""
    out = []
    for case in live_cases(conn, run):
        rec = cases.active_rec(conn, case)
        if rec is None:
            continue
        out += [(case, rec, f) for f in commitment.checks_in_force(case, rec)
                if f["kind"] in signals.COUNT_KINDS and f["changes_plan"]]
    return out


def transfer_today(ref: refdata.Ref, run: dict, rec: dict | None) -> dict | None:
    """The version's first transfer option (the engine's rank order) on a run that runs on the run's day."""
    if rec is None:
        return None
    return next((o for o in rec["result"]["options"] if o["kind"] == TRANSFER and o["run_code"]
                 and ref.run_on(o["run_code"], run["day"]) is not None), None)


def anchor(conn, run: dict, ref: refdata.Ref, first: dict | None) -> tuple[dict, dict] | None:
    """(case, option) the reports go to: the first candidate's case when it has a transfer option today, else the
    first live non-judge case that has one (D-39); None when no case has one."""
    for case in ([first] if first else []) + live_cases(conn, run):
        option = transfer_today(ref, run, cases.active_rec(conn, case))
        if option is not None:
            return case, option
    return None


def photo_asset() -> str:
    """`asset://<name>` of the samples' photo-only answer file (data/demo_signals in a build; the test fixture folder
    in the tests). A missing file stops the build: the photo is the team's own (D-19, 07-02), never made up."""
    entry = next(e for e in samples() if e["id"] == PHOTO_ONLY)
    return files.asset_uri(entry["file"])


def samples() -> list[dict]:
    """The samples in DEMO_SIGNALS_DIR/samples.json (the guide's texts, 07-02)."""
    return json.loads((pathlib.Path(config.DEMO_SIGNALS_DIR) / "samples.json").read_text(encoding="utf-8"))


def _reports(conn, run: dict, ref: refdata.Ref, case: dict, option: dict) -> list[dict]:
    """The CAPACITY report at the option's destination and the TRANSPORT quote for its run, as their owners file
    them; a destination with no capacity row today gets no CAPACITY report (nothing is made up)."""
    day, dest, code = run["day"], option["dest"], option["run_code"]
    today = cases.iso_of(run, day)
    zone = ref.products[case["sku"]].zone
    out = []
    free = ref.free_totes(dest, zone, day)
    if free is not None:
        half = free // 2
        out.append(reports.create(
            conn, run, _manager(dest), "CAPACITY",
            {"zone": zone, "free_totes": half, "valid_from": today, "valid_to": today},
            f"Only {half} totes of {zone} space free at {dest} on {today}.", case_id=case["case_id"]))
    trip = ref.run_on(code, day)
    out.append(reports.create(
        conn, run, HQ, "TRANSPORT",
        {"run_code": code, "what": "QUOTE", "amount": float(trip.fixed_cost), "pricing_basis": "PER_TRIP",
         "includes_handling": "YES", "applies_on": today, "valid_until": today},
        f"Carrier quote for {code} on {today}: {cases.money(ref, trip.fixed_cost)} per trip, handling included.",
        case_id=case["case_id"]))
    return out


def generate(conn, run: dict) -> dict:
    """File the template's preloaded material by the rule (module docstring) and log the run-level `preload` entry:
    {rule, requests, reports, missing}. Returns the entry. Called by the template build only."""
    ref = refdata.get(run["dataset_version"])
    found = candidates(conn, run)
    requests = []
    for role, (case, rec, fact) in zip(ROLES, found):
        view = signals.request_evidence(conn, run, _manager(case["source_branch"]), case["case_id"],
                                        rec["rec_version"], fact["fact_id"])
        if role == "photo_only":
            signals.answer(conn, run, _manager(view["to_branch"]), view["request_id"], None, None, "now",
                           asset=photo_asset())
        requests.append({"role": role, "case_id": case["case_id"], "fact_id": fact["fact_id"],
                         "lot_ids": view["lot_ids"], "to_branch": view["to_branch"],
                         "request_id": view["request_id"]})
    ids = [q["request_id"] for q in requests]
    db.run(conn, "UPDATE evidence_requests SET provenance = :p WHERE run_id = :r AND request_id = ANY(:q)",
           p=SIMULATED, r=run["run_id"], q=ids)
    db.run(conn, "UPDATE signals SET provenance = :p WHERE run_id = :r AND request_id = ANY(:q)",
           p=SIMULATED, r=run["run_id"], q=ids)
    filed = []
    at = anchor(conn, run, ref, found[0][0] if found else None)
    if at is not None:
        filed = _reports(conn, run, ref, *at)
        db.run(conn, "UPDATE signals SET provenance = :p WHERE run_id = :r AND signal_id = ANY(:s)",
               p=SIMULATED, r=run["run_id"], s=[v["signal_id"] for v in filed])
    db.run(conn, "UPDATE demo_runs SET preloaded_at = now() WHERE run_id = :r", r=run["run_id"])
    entry = {"rule": RULE, "requests": requests,
             "reports": [{"kind": v["kind"], "branch": v["branch"], "case_id": v["case_id"],
                          "signal_id": v["signal_id"], "fields": v["fields"]} for v in filed],
             "missing": minimums(conn, run)}
    db.log(conn, run["run_id"], run["day"], LOG, None, **entry)
    return entry


def minimums(conn, run: dict) -> list[str]:
    """Each D-18 minimum the run's preloaded rows don't meet, named (`MISSING`, in its order); empty when all hold."""
    rid = run["run_id"]
    asked = db.rows(conn, "SELECT q.state, q.answer_signal_id, s.file_uri, s.fields FROM evidence_requests q "
                          "LEFT JOIN signals s ON s.run_id = q.run_id AND s.signal_id = q.answer_signal_id "
                          "WHERE q.run_id = :r AND q.provenance = :p", r=rid, p=SIMULATED)
    filed = db.rows(conn, "SELECT kind FROM signals WHERE run_id = :r AND provenance = :p AND report_id IS NOT NULL "
                          "AND state = 'READ'", r=rid, p=SIMULATED)
    owners = {reports.KINDS[f["kind"]]["owner"] for f in filed}
    met = {"unanswered": any(q["state"] == "OPEN" and q["answer_signal_id"] is None for q in asked),
           "photo_only": any(q["state"] == "OPEN" and q["file_uri"] and (q["fields"] or {}).get("needs_input")
                             for q in asked),
           "manager": "manager" in owners, "hq": "hq" in owners}
    return [MISSING[k] for k in MISSING if not met[k]]


def rebase(conn, new_run_id: str, template: dict) -> None:
    """Shift every preloaded time of a fresh clone by one offset, its now() minus the template's preloaded_at
    (07-CONTEXT D-05, D-18): the preloaded requests' asked and due times (and the "Respond by" in their asks), the
    preloaded answers' and reports' filed and checked times, and the activity entries that filed them. A request is
    then due exactly requests.response_min minutes after the workspace was created. Nothing else changes, and nothing
    is generated, answered or filed: a handful of rows in bounded UPDATEs, inside the 2 s reset (T-07-60)."""
    at = template.get("preloaded_at")
    if at is None:  # nothing preloaded (a template built before 07-16)
        return
    shift = realtime.db_now(conn) - at  # now() is fixed in the transaction, so the SQL below moves by this too
    n, p = new_run_id, SIMULATED
    for row in db.rows(conn, "SELECT DISTINCT due_at FROM evidence_requests WHERE run_id = :n AND provenance = :p "
                             "AND due_at IS NOT NULL", n=n, p=p):  # one in practice: one now() asked them all
        due = row["due_at"]
        db.run(conn, "UPDATE evidence_requests SET ask_text = replace(ask_text, :old, :new) WHERE run_id = :n "
                     "AND provenance = :p AND due_at = :d", n=n, p=p, d=due,
               old=f"Respond by {realtime.label(due)}", new=f"Respond by {realtime.label(due + shift)}")
    db.run(conn, "UPDATE evidence_requests SET created_at = created_at + (now() - :a), due_at = due_at + (now() - :a) "
                 "WHERE run_id = :n AND provenance = :p", a=at, n=n, p=p)
    db.run(conn, "UPDATE signals SET created_at = created_at + (now() - :a), observed_at = observed_at + (now() - :a) "
                 "WHERE run_id = :n AND provenance = :p", a=at, n=n, p=p)
    db.run(conn, "UPDATE activity_log SET at = at + (now() - :a) WHERE run_id = :n AND at = :a AND type = ANY(:t)",
           a=at, n=n, t=list(FILED))


def shortfall(conn, run_id: str) -> list[str]:
    """The minimums the run's `preload` entry names as not met (D-39), for the build's output and the release check;
    every minimum when the run has no entry (it was built before 07-16, or never preloaded)."""
    row = db.one(conn, "SELECT payload FROM activity_log WHERE run_id = :r AND type = :t AND case_id IS NULL "
                       "ORDER BY seq DESC LIMIT 1", r=run_id, t=LOG)
    return list(MISSING.values()) if row is None else list(row["payload"]["missing"])


# --- the reviewer guide (07-CONTEXT D-19) -----------------------------------------------------------------------------

GUIDE_FILES = ("guide-samples.json", "quote.pdf")  # all the guide command writes, inside its --out folder
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def judge_case(conn, run: dict) -> dict | None:
    """The judge's case: the story case, else a public set's verify-before-move case (the first live one)."""
    for tag in JUDGE_TAGS:
        case = db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND tag = :t AND state = ANY(:s) "
                            "ORDER BY length(case_id), case_id LIMIT 1", r=run["run_id"], t=tag,
                      s=list(cases.LIVE_STATES))
        if case is not None:
            return case
    return None


def guide_values(conn, run: dict, *, case_key: str | None = None) -> tuple[dict, dict]:
    """(the judge's case, the guide's placeholder values) from the template's own codes: `source_store` the case's
    store, `dest_store` and `run_code` its recommended transfer (else its first transfer option on today's run),
    `store` the branch its count fact asks (the store that answers; else the case's store), `product` "Name · Code"
    where a name is mapped (D-03), `lot_id` the case's lot, `weekday` and `date` the business day. A template with no
    judge's case or no transfer run is a ValueError naming what is missing; nothing is made up. An explicit
    case_key is looked up only in this template's live cases and uses its plan in force."""
    ref = refdata.get(run["dataset_version"])
    case = (judge_case(conn, run) if case_key is None else db.one(
        conn, "SELECT * FROM cases WHERE run_id=:r AND key=:k AND state=ANY(:s)",
        r=run["run_id"], k=case_key, s=list(cases.LIVE_STATES)))
    if case is None:
        raise ValueError("the template has no matching live guide case")
    rec = cases.active_rec(conn, case)
    if rec is None:
        raise ValueError(f"{case['key']} has no recommendation")
    if case_key is None:
        key = rec["result"]["recommended"]
    else:
        from . import selection
        key, _, _ = selection.plan_in_force(case, rec)
    plan = cases.parse_plan(key)
    if plan.kind == TRANSFER and ref.run_on(plan.run_code, run["day"]) is not None:
        dest, code = plan.dest, plan.run_code
    else:
        option = transfer_today(ref, run, rec)
        if option is None:
            raise ValueError(f"{case['key']} has no transfer option on today's run")
        dest, code = option["dest"], option["run_code"]
    fact = next((f for f in commitment.checks_in_force(case, rec) if f["kind"] in signals.COUNT_KINDS), None)
    d = ref.date(run["day"])
    return case, {"store": fact["branch"] if fact else case["source_branch"], "source_store": case["source_branch"],
                  "dest_store": dest, "product": signals.product_label(ref, case["sku"]), "lot_id": case["lot_id"],
                  "run_code": code, "weekday": cases.weekday(run["day"], ref),
                  "date": f"{d.day} {realtime.MONTHS[d.month - 1]} {d.year}"}


def render_guide(conn, template: dict, *, case_key: str | None = None, ambiguous_quote: bool = False) -> dict:
    """The reviewer guide's material from this template (D-19): {template, dataset_version, case, values, samples,
    quote}. Each sample keeps its id, use, kind and file, with its title, text and expected result filled from
    `guide_values`; a placeholder the values lack is a ValueError (the samples and the code disagree). `quote` is the
    run's own fixed cost from the data in the reference currency (the quote PDF and the quote screenshot)."""
    ref = refdata.get(template["dataset_version"])
    case, values = guide_values(conn, template, case_key=case_key)

    def fill(text: str) -> str:
        unknown = sorted(set(PLACEHOLDER.findall(text)) - set(values))
        if unknown:
            raise ValueError(f"samples.json uses {{{unknown[0]}}}, which the guide does not fill")
        return PLACEHOLDER.sub(lambda m: values[m.group(1)], text)

    trip = ref.run_on(values["run_code"], template["day"])
    guide = {"template": template["run_id"], "dataset_version": template["dataset_version"], "case": case["key"],
            "values": values,
            "samples": [{"id": e["id"], "use": e["use"], "kind": e["kind"], "file": e["file"],
                         "title": fill(e["title"]), "text": fill(e["text"]), "expected": fill(e["expected"])}
                        for e in samples()],
            "quote": {"run_code": values["run_code"], "amount": float(trip.fixed_cost), "currency": ref.currency}}
    quote = guide["quote"]
    amount = (f"${quote['amount']:.2f}" if quote["currency"] == "USD"
              else f"{quote['amount']:.2f} {quote['currency']}")
    terms = ("; pricing basis, handling inclusion and expiry unknown" if ambiguous_quote
             else " per trip, handling included")
    for sample in guide["samples"]:
        if sample["id"] == "quote-pdf":
            sample["expected"] = f"a quote for {quote['run_code']}: {amount}{terms} (TRANSPORT)"
    return guide


def _pdf_text(text: str) -> str:
    """A PDF literal string's body: backslash and parentheses escaped, ASCII only (the middle dot written as '-')."""
    text = text.replace("·", "-").encode("ascii", "replace").decode("ascii")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def quote_pdf(run_code: str, amount: float, currency: str, *, origin: str | None = None, dest: str | None = None,
              trip_date: str | None = None, ambiguous: bool = False) -> bytes:
    """A minimal one-page PDF of a synthetic carrier quote, written in code (no dependency): the run code (and its
    route and trip date when given), the amount per trip with handling included, and "Synthetic sample". It names no
    carrier and no retailer. ambiguous=True omits pricing and handling terms for the evaluation's
    deliberately incomplete quote; ordinary guide quotes keep their stated terms."""
    route = f"Route: {run_code}" + (f" {origin} to {dest}" if origin and dest else "")
    lines = ["QUOTATION - transport", route] + ([f"Date of trip: {trip_date}"] if trip_date else []) + [
        f"Price: {amount:.2f} {currency}" + ("" if ambiguous else " per trip, handling included"),
        "Synthetic sample for the reviewer guide, not a real quote"]
    text = "BT /F1 14 Tf 60 760 Td 20 TL " + " ".join(f"({_pdf_text(x)}) Tj T*" for x in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(text)} >>\nstream\n{text}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode("ascii")
    xref = len(out)
    out += (f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
            ).encode("ascii")
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")


def quote_for_run(run: dict, run_code: str, *, ambiguous: bool = False) -> tuple[dict, bytes]:
    """The guide and evaluation share these exact dataset-derived quote bytes."""
    ref = refdata.get(run["dataset_version"])
    trip = ref.run_on(run_code, run["day"])
    if trip is None:
        raise ValueError("the quoted run is not scheduled on the template day")
    day = ref.date(run["day"])
    trip_date = f"{cases.weekday(run['day'], ref)} {day.day} {realtime.MONTHS[day.month - 1]} {day.year}"
    quote = {"run_code": run_code, "amount": float(trip.fixed_cost), "currency": ref.currency}
    pdf = quote_pdf(run_code, quote["amount"], ref.currency, origin=trip.origin, dest=trip.dest,
                    trip_date=trip_date, ambiguous=ambiguous)
    return quote, pdf


def main(argv: list[str] | None = None) -> int:
    """python -m api.preload guide --out DIR: the ACTIVE template's guide samples (guide-samples.json) and quote PDF
    (quote.pdf) written into DIR, nothing else and nowhere else; prints the run code the quote screenshot must show.
    --template also accepts an inactive READY template; --case-key selects its live case. The explicit
    --ambiguous-quote option omits pricing and handling terms. Reads the database only; no cloud call."""
    ap = argparse.ArgumentParser(prog="python -m api.preload", description="The reviewer guide from the template")
    sub = ap.add_subparsers(dest="command", required=True)
    g = sub.add_parser("guide", help="write the guide's samples and quote PDF from the ACTIVE template")
    g.add_argument("--out", required=True, help="the folder to write guide-samples.json and quote.pdf into")
    g.add_argument("--template", help="read this READY or ACTIVE template without activating it")
    g.add_argument("--case-key", help="use this live case from the selected template")
    g.add_argument("--ambiguous-quote", action="store_true",
                   help="omit pricing basis and handling terms for the evaluation ambiguity scenario")
    args = ap.parse_args(argv)
    from . import sandbox  # it imports this module
    with db.tx() as conn:
        template = (sandbox.active_template(conn) if args.template is None else
                    db.one(conn, "SELECT * FROM demo_runs WHERE run_id=:r", r=args.template))
        if template is None or template["kind"] != "TEMPLATE" or template["status"] not in ("READY", "ACTIVE"):
            print("no ready reference template: select a READY or ACTIVE template", file=sys.stderr)
            return 1
        try:
            guide = render_guide(conn, template, case_key=args.case_key, ambiguous_quote=args.ambiguous_quote)
            quote, pdf = quote_for_run(template, guide["quote"]["run_code"], ambiguous=args.ambiguous_quote)
            if quote != guide["quote"]:
                raise ValueError("guide quote does not match its dataset run")
        except ValueError as exc:
            print(f"guide not rendered: {exc}", file=sys.stderr)
            return 1
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    q = guide["quote"]
    (out / GUIDE_FILES[0]).write_text(json.dumps(guide, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / GUIDE_FILES[1]).write_bytes(pdf)
    print(f"wrote {', '.join(GUIDE_FILES)} to {out} from template {template['run_id']} ({guide['case']})")
    print(f"quote screenshot run code: {q['run_code']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
