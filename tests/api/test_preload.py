"""Preloaded simulated requests, responses and reports (SIG-05; 07-CONTEXT D-05, D-18, D-19, D-39).

The template build files them by a stated rule from its own cases (api/preload.py); every workspace gets its copy
with its own deadlines, and nothing is filed or answered after the workspace is created. On both test templates the
only countable fact is the judge's case (D-39), so the real builds file the reports and report the request minimums
they could not meet; the request mechanics are tested by enabling a candidate inside the test (the judge's case
allowed as one), never by padding or editing the data.
"""
from __future__ import annotations

import pytest

from api import cases, config, db, realtime, sandbox, signals
from tests.api.conftest import drop_runs, fresh

MISSING_UNANSWERED = "no preloaded unanswered request"
MISSING_PHOTO = "no preloaded photo-only response"
MISSING_MANAGER = "no preloaded unconfirmed report for a store manager"
MISSING_HQ = "no preloaded unconfirmed report for HQ"
JUDGE = (cases.STORY_TAG, "verify")  # the judge's story case; a public set's verify-before-move case


def preload_entry(run_id: str) -> dict | None:
    """The run-level `preload` activity entry the template build wrote."""
    with db.tx() as conn:
        row = db.one(conn, "SELECT payload FROM activity_log WHERE run_id = :r AND type = 'preload' "
                           "AND case_id IS NULL ORDER BY seq DESC LIMIT 1", r=run_id)
    return row["payload"] if row else None


def simulated(run_id: str, table: str) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, f"SELECT * FROM {table} WHERE run_id = :r AND provenance = 'simulated' "
                             f"ORDER BY created_at, 2", r=run_id)


def reactivate(template: str, built: str) -> None:
    """The session's synthetic template ACTIVE again, the scratch one RETIRED, in one transaction."""
    with db.tx() as conn:
        db.run(conn, "UPDATE demo_runs SET status = 'RETIRED' WHERE run_id = :b", b=built)
        db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE' WHERE run_id = :t", t=template)


def clone_now(template_run: str) -> tuple[dict, object]:
    """A workspace cloned now from this template run (ACTIVE or not), and the database's now() of the clone."""
    with db.tx() as conn:
        rid = sandbox.clone(conn, fresh(template_run))
        now = realtime.db_now(conn)
    return fresh(rid), now


@pytest.fixture(scope="module")
def scratch(template):
    """Scratch templates of the synthetic set built with a candidate enabled inside the test (D-39): the judge's case
    is allowed as a candidate, since its count fact is the only one the data gives. `unanswered` and `again` keep the
    rule's roles (the one candidate gets the unanswered request); `photo_only` gives that candidate the photo-only
    role. Every caps.spend call during the builds is recorded in `spent`. The session template is ACTIVE again after
    each build; the scratch runs and their clones are dropped at teardown."""
    from api import caps, preload
    dv, built, spent = fresh(template)["dataset_version"], {}, []
    real_spend, roles = caps.spend, preload.ROLES
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(caps, "spend", lambda *a, **k: (spent.append(a), real_spend(*a, **k))[1])
        mp.setattr(preload, "JUDGE_TAGS", ())
        for name, given in (("unanswered", roles), ("again", roles), ("photo_only", ("photo_only",))):
            mp.setattr(preload, "ROLES", given)
            built[name] = sandbox.build_template(dv)
            reactivate(template, built[name])
    yield {**built, "spent": spent}
    with db.tx() as conn:
        ids = list(built.values())
        clones = [r["run_id"] for r in db.rows(conn, "SELECT run_id FROM demo_runs WHERE template_run_id = ANY(:t)",
                                               t=ids)]
        drop_runs(conn, [*ids, *clones])


def test_preload_minimums_and_labels(template, request):
    """SIG-05, D-18, D-39: the real template files a branch report a store manager owns and a transport quote HQ owns,
    both READ and labelled, never on the judge's case; it names the two request minimums its data can't meet in the
    `preload` entry and is ACTIVE all the same (nothing padded). With a candidate enabled, the same rule files an
    unanswered request, or a request answered with the packaged photo only ("Photo received · count missing"), each
    labelled; no build spends a cap."""
    entry = preload_entry(template)
    assert entry is not None, "the template build logs its preload entry"
    assert entry["missing"] == [MISSING_UNANSWERED, MISSING_PHOTO]
    assert entry["requests"] == []
    assert fresh(template)["status"] == "ACTIVE"
    assert simulated(template, "evidence_requests") == []
    with db.tx() as conn:
        judge = {c["case_id"] for c in db.rows(conn, "SELECT case_id FROM cases WHERE run_id = :r AND tag = ANY(:t)",
                                               r=template, t=list(JUDGE))}
    reports = simulated(template, "signals")
    assert sorted((r["kind"], r["state"], r["revision"]) for r in reports) == [("CAPACITY", "READ", 1),
                                                                               ("TRANSPORT", "READ", 1)]
    assert all(r["report_id"] and r["case_id"] not in judge for r in reports)
    assert {r["kind"]: r["reporter_role"] for r in reports} == {"CAPACITY": "manager", "TRANSPORT": "hq"}

    run, _ = clone_now(template)
    from api import reports as rp
    with db.tx() as conn:
        views = [rp.report_view(conn, run["run_id"], r) for r in simulated(run["run_id"], "signals")]
    assert {(v["kind"], v["provenance"], v["simulated_label"]) for v in views} == {
        ("CAPACITY", "simulated", "Simulated report"), ("TRANSPORT", "simulated", "Simulated report")}
    assert {v["kind"]: v["owner"]["role"] for v in views} == {"CAPACITY": "manager", "TRANSPORT": "hq"}

    built = request.getfixturevalue("scratch")
    assert built["spent"] == []  # preloaded material spends no allowance and no cap (D-20)
    a, b = built["unanswered"], built["photo_only"]
    assert preload_entry(a)["missing"] == [MISSING_PHOTO]
    assert preload_entry(b)["missing"] == [MISSING_UNANSWERED]
    [qa] = simulated(a, "evidence_requests")
    assert (qa["state"], qa["answer_signal_id"], qa["fact_kind"]) == ("OPEN", None, "PHANTOM_STOCK")
    [qb] = simulated(b, "evidence_requests")
    with db.tx() as conn:
        answer = db.one(conn, "SELECT * FROM signals WHERE run_id = :r AND signal_id = :s", r=b,
                        s=qb["answer_signal_id"])
    assert (qb["state"], answer["provenance"], answer["file_uri"]) == ("OPEN", "simulated", "asset://fixture-photo.jpg")
    assert answer["file_counted"] is False and answer["fields"]["needs_input"] and answer["text"] == ""
    for run_id in (a, b):
        assert sorted(r["kind"] for r in simulated(run_id, "signals") if r["report_id"]) == ["CAPACITY", "TRANSPORT"]

    wa, _ = clone_now(a)
    wb, _ = clone_now(b)
    with db.tx() as conn:
        [va] = [signals.request_view(conn, wa, q, q["case_id"]) for q in simulated(wa["run_id"], "evidence_requests")]
        [vb] = [signals.request_view(conn, wb, q, q["case_id"]) for q in simulated(wb["run_id"], "evidence_requests")]
    assert (va["provenance"], va["simulated_label"], va["status_kind"]) == ("simulated", "Simulated request",
                                                                            "AWAITING")
    assert (vb["status_kind"], vb["status"]) == ("PHOTO_PARTIAL", "Photo received · count missing")
    assert (vb["answer"]["provenance"], vb["answer"]["simulated_label"]) == ("simulated", "Simulated response")
    assert vb["answer"]["file"]["type"] == "image/jpeg"


def test_a_fresh_workspace_starts_its_own_deadlines(scratch):
    """D-05, D-18: in a workspace cloned now, every preloaded OPEN request reads "Awaiting response" with its full
    requests.response_min minutes, asked at the clone's own now() and asking to respond by its own deadline."""
    run, now = clone_now(scratch["unanswered"])
    rows = [q for q in simulated(run["run_id"], "evidence_requests") if q["state"] == "OPEN"]
    assert rows
    with db.tx() as conn:
        views = [signals.request_view(conn, run, q, q["case_id"]) for q in rows]
    for q, v in zip(rows, views):
        assert v["status_kind"] == "AWAITING", v["status"]
        assert v["minutes_left"] in (config.REQUEST_RESPONSE_MIN, config.REQUEST_RESPONSE_MIN - 1)
        assert abs((q["created_at"] - now).total_seconds()) < 1
        assert v["ask_text"].endswith(f"Respond by {realtime.label(q['due_at'])}.")


# --- the time shift at clone; determinism; parallel clones; a missing minimum is reported (Task 2) -------------------

PRELOADED_TYPES = ("evidence_requested", "evidence_answered", "report_filed")


def by(run_id: str, table: str, key: str) -> dict:
    with db.tx() as conn:
        return {r[key]: r for r in db.rows(conn, f"SELECT * FROM {table} WHERE run_id = :r", r=run_id)}


def test_clone_shifts_every_preloaded_time(scratch):
    """D-05, D-18: a clone's preloaded requests (created_at, due_at), their answers and the preloaded reports
    (created_at, observed_at) and the activity entries that filed them (at) all move by one offset, the clone's now()
    minus the template's preloaded_at; every other row keeps its times; a request stays due exactly
    requests.response_min minutes after it was asked."""
    from datetime import timedelta
    tpl = fresh(scratch["photo_only"])
    run, now = clone_now(tpl["run_id"])
    shift = now - tpl["preloaded_at"]
    assert shift > timedelta(0)
    moved = 0
    for table, key, cols in (("evidence_requests", "request_id", ("created_at", "due_at")),
                             ("signals", "signal_id", ("created_at", "observed_at"))):
        theirs, mine = by(tpl["run_id"], table, key), by(run["run_id"], table, key)
        assert set(mine) == set(theirs)
        for k, row in mine.items():
            for col in cols:
                was = theirs[k][col]
                if row["provenance"] == "simulated" and was is not None:
                    assert row[col] - was == shift, (table, k, col)
                    moved += 1
                else:
                    assert row[col] == was, (table, k, col)
    assert moved >= 6  # the request (2), its answer (2) and two reports (1 each)
    for q in simulated(run["run_id"], "evidence_requests"):
        assert q["due_at"] - q["created_at"] == timedelta(minutes=config.REQUEST_RESPONSE_MIN)
        assert q["created_at"] == now
    theirs, mine = by(tpl["run_id"], "activity_log", "seq"), by(run["run_id"], "activity_log", "seq")
    shifted = [s for s, a in mine.items() if a["at"] != theirs[s]["at"]]
    assert shifted and all(mine[s]["at"] - theirs[s]["at"] == shift and mine[s]["type"] in PRELOADED_TYPES
                           for s in shifted)
    assert all(a["type"] not in PRELOADED_TYPES or s in shifted for s, a in mine.items())


def test_preload_is_deterministic(scratch):
    """Edge SIG-05 ordering and idempotency: two builds of the same data preload the same items (cases, facts, lots,
    fields and texts); only their ids and times differ."""
    def items(run_id):
        entry = preload_entry(run_id)
        requests = [{k: v for k, v in q.items() if k != "request_id"} for q in entry["requests"]]
        filed = [{k: v for k, v in r.items() if k != "signal_id"} for r in entry["reports"]]
        rows = sorted((s["kind"], s["branch"], s["case_id"], s["text"], str(s["fields"]), s["state"])
                      for s in simulated(run_id, "signals"))
        asked = sorted((q["case_id"], q["fact_id"], str(q["lot_ids"]), q["to_branch"], q["state"])
                       for q in simulated(run_id, "evidence_requests"))
        return requests, filed, rows, asked, entry["missing"]
    assert items(scratch["unanswered"]) == items(scratch["again"])
    assert preload_entry(scratch["unanswered"])["requests"]  # a candidate was enabled: there is something to compare


def test_two_clones_at_once(scratch):
    """Edge SIG-05 concurrency: two workspaces cloned at once each get their own copy of the preloaded rows, with times
    shifted to their own creation."""
    import threading
    tpl = fresh(scratch["photo_only"])
    gate, out, errors = threading.Barrier(2), [], []

    def one():
        try:
            gate.wait()
            out.append(db.in_tx(lambda conn: (sandbox.clone(conn, tpl), realtime.db_now(conn))))
        except Exception as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len({rid for rid, _ in out}) == 2
    for rid, now in out:
        [q] = simulated(rid, "evidence_requests")
        assert q["created_at"] == now
        answer = by(rid, "signals", "signal_id")[q["answer_signal_id"]]
        assert answer["observed_at"] == now and answer["created_at"] == now
    [a, b] = [rid for rid, _ in out]
    assert {r["request_id"] for r in simulated(a, "evidence_requests")} == \
        {r["request_id"] for r in simulated(b, "evidence_requests")}  # the same template rows, copied into each


def test_a_missing_minimum_is_reported_never_a_failure(template, monkeypatch, capsys):
    """D-39 (replacing the plan's "fails the build"): with the rule finding no candidate and no report anchor, the
    template is ACTIVE all the same, nothing is filed to meet a minimum, the `preload` entry names all four, and the
    build command prints them."""
    from api import preload, refdata
    dv = fresh(template)["dataset_version"]
    with monkeypatch.context() as mp:
        mp.setattr(preload, "candidates", lambda conn, run: [])
        mp.setattr(preload, "anchor", lambda conn, run, ref, first: None)
        built = sandbox.build_template(dv)
    try:
        t = fresh(built)
        assert (t["status"], t["failure"]) == ("ACTIVE", None)
        four = [MISSING_UNANSWERED, MISSING_PHOTO, MISSING_MANAGER, MISSING_HQ]
        assert preload_entry(built)["missing"] == four
        assert simulated(built, "evidence_requests") == [] and simulated(built, "signals") == []
        with db.tx() as conn:
            assert preload.shortfall(conn, built) == four
            assert preload.shortfall(conn, template) == [MISSING_UNANSWERED, MISSING_PHOTO]
        monkeypatch.setattr(refdata, "install", lambda *a, **k: dv)
        monkeypatch.setattr(sandbox, "build_template", lambda version, *args, **kwargs: built)
        assert sandbox.main(["build", "unused", "--allow-local"]) == 0
        assert f"preload: missing {'; '.join(four)}" in capsys.readouterr().out
    finally:
        reactivate(template, built)
        with db.tx() as conn:
            drop_runs(conn, [built])


def test_the_public_template_reports_its_shortfall(template, public_dv):
    """D-39 on the public set: its only countable fact is the verify-before-move case's (the judge's), so its build
    files no request, names the two request minimums, and files nothing on the judge's case."""
    built = sandbox.build_template(public_dv)
    try:
        entry = preload_entry(built)
        assert entry["requests"] == [] and simulated(built, "evidence_requests") == []
        assert entry["missing"][:2] == [MISSING_UNANSWERED, MISSING_PHOTO]
        with db.tx() as conn:
            judge = {c["case_id"] for c in db.rows(conn, "SELECT case_id FROM cases WHERE run_id = :r "
                                                         "AND tag = ANY(:t)", r=built, t=list(JUDGE))}
        assert judge  # the catalogue tagged its verify-before-move case
        assert all(s["case_id"] not in judge for s in simulated(built, "signals"))
        assert set(entry["missing"][2:]) <= {MISSING_MANAGER, MISSING_HQ}
    finally:
        reactivate(template, built)
        with db.tx() as conn:
            clones = [r["run_id"] for r in db.rows(conn, "SELECT run_id FROM demo_runs WHERE template_run_id = :r",
                                                   r=built)]
            drop_runs(conn, [built, *clones])


# --- the reviewer guide rendered from the template (Task 3; D-19) ------------------------------------------------------

def test_the_guide_renders_from_the_template(template, tmp_path, capsys):
    """D-19: every sample's title, text and expected result is filled from the ACTIVE template's own codes (the story
    case's source and destination stores, its product code, its lot, a run in its input), with no "{" left, in
    English and with no retailer name; the quote PDF is a minimal PDF naming the run code and the run's cost from the
    data and saying "Synthetic sample"; the CLI writes exactly guide-samples.json and quote.pdf into DIR and prints the
    run code the quote screenshot must show."""
    import json
    from api import preload, refdata
    from tests.api.test_demo_signals import denylist, latin_only
    with db.tx() as conn:
        tpl = sandbox.active_template(conn)
        assert tpl["run_id"] == template
        guide = preload.render_guide(conn, tpl)
        story = db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND tag = :t", r=template, t=cases.STORY_TAG)
        rec = cases.active_rec(conn, story)
    ref = refdata.get(tpl["dataset_version"])
    plan = cases.parse_plan(rec["result"]["recommended"])
    values = guide["values"]
    assert guide["case"] == story["key"]
    assert (values["source_store"], values["dest_store"], values["lot_id"]) == (story["source_branch"], plan.dest,
                                                                                story["lot_id"])
    assert story["sku"] in values["product"] and values["run_code"] == plan.run_code
    assert ref.run_on(values["run_code"], tpl["day"]) is not None
    assert len(guide["samples"]) == len(preload.samples())
    banned = [n.lower() for n in denylist()]
    for entry in guide["samples"]:
        for key in ("title", "text", "expected"):
            assert "{" not in entry[key] and "}" not in entry[key], (entry["id"], key)
            assert latin_only(entry[key]) and not any(n in entry[key].lower() for n in banned), (entry["id"], key)
    chiller = next(e for e in guide["samples"] if e["id"] == "chiller-capacity")
    assert chiller["title"] == f"Chiller down at {plan.dest}"
    trip = ref.run_on(plan.run_code, tpl["day"])
    assert guide["quote"] == {"run_code": plan.run_code, "amount": float(trip.fixed_cost), "currency": ref.currency}

    pdf = preload.quote_pdf("R-02", 85.0, "USD")
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
    assert b"R-02" in pdf and b"85.00 USD" in pdf and b"Synthetic sample" in pdf

    out = tmp_path / "guide"
    assert preload.main(["guide", "--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["guide-samples.json", "quote.pdf"]
    written = json.loads((out / "guide-samples.json").read_text(encoding="utf-8"))
    assert written["samples"] == guide["samples"] and written["values"] == values
    quote = (out / "quote.pdf").read_bytes()
    assert quote.startswith(b"%PDF-") and plan.run_code.encode() in quote
    assert f"{trip.fixed_cost:.2f} {ref.currency}".encode() in quote
    assert f"quote screenshot run code: {plan.run_code}" in capsys.readouterr().out
