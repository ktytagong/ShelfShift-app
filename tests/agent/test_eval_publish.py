"""DB-backed eval publication, with inherited offline/paid-client guards intact."""
from __future__ import annotations

import importlib.util
import csv
import io
import json
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

# Import test configuration before api.config is loaded. The inherited agent
# schema/template fixtures own the DB lifecycle; importing fixtures here would
# register separate session fixtures and drop other agent tests' cached template.
from tests.api import conftest as api_fixtures  # noqa: F401
from api import db, sandbox

FINGERPRINT = "a" * 64


def store_module():
    assert importlib.util.find_spec("agent.eval") is not None, "The eval publication package must exist"
    from agent.eval import store
    return store


@pytest.fixture(autouse=True)
def empty_evals(schema):
    # Before RED the tables do not exist; never obscure the planned assertion.
    with db.tx() as conn:
        for table in ("eval_publications", "eval_items", "eval_attempts", "eval_runs"):
            if db.one(conn, "SELECT to_regclass(:t) AS t", t=table)["t"]:
                db.run(conn, f"DELETE FROM {table}")


def recorded(conn, eval_id="eval-test", *, status="COMPLETE", started_at="2026-10-17T10:00:00Z"):
    s = store_module()
    s.record_run(conn, eval_id=eval_id, fingerprint=FINGERPRINT, model_id="offline-model",
                 build_sha="b" * 40, assistant_version="assistant-test", engine_version="engine-2",
                 dataset_version="test-data", status=status, started_at=started_at,
                 finished_at=started_at, usage={"calls": 30, "tokens": 1234, "cost_usd": 9},
                 reviewed_by="private-reviewer", reviewed_at=started_at)
    for scenario in range(1, 11):
        for run in range(1, 4):
            s.record_attempt(conn, eval_id=eval_id, scenario_id=scenario, run_no=run,
                             workspace_run_id=f"workspace-{scenario}-{run}", valid=True,
                             model_calls=1, cache_hits=0, status="COMPLETE", evidence_ref="private-evidence")
            items = [dict(item_id="quality", grp="assistant" if scenario <= 5 else "reading",
                          gate=None, facet="quality", scorer="human", result="PASS", note="",
                          item=f"Scenario {scenario} quality", reference="Expected conclusion", actual="Actual answer")]
            if scenario == 1:
                for gate in ("unsourced", "self_approval", "hidden_truth"):
                    items.extend([
                        dict(item_id=f"{gate}-behaviour", grp="safety", gate=gate, facet="behaviour",
                             scorer="script", result="FAIL" if gate == "self_approval" and run == 1 else "PASS",
                             note="The model attempted approval; the server stopped it."),
                        dict(item_id=f"{gate}-protection", grp="safety", gate=gate, facet="protection",
                             scorer="script", result="PASS", note="")])
            s.record_items(conn, eval_id, scenario, run, items)
    return eval_id


def public_response():
    from api.app import app
    with TestClient(app) as client:
        response = client.get("/api/eval/summary")
        assert response.status_code == 200, response.text
        assert not client.cookies
        return response.json()


def test_published_eval_reaches_the_public_route():
    s = store_module()
    with db.tx() as conn:
        eval_id = recorded(conn)
        s.publish(conn, eval_id, FINGERPRINT)
    view = public_response()
    published = view["published"]
    assert published["label"] == "Preliminary evaluation · 10 scenarios × 3 runs"
    assert [g["name"] for g in published["groups"]] == ["Safety", "Assistant", "Reading"]
    assert published["build"] == "b" * 12 and published["fingerprint"] == "a" * 12
    assert published["usage"] == {"calls": 30, "tokens": 1234}
    assert view["latest_unpublished"] is None
    assistant = published["groups"][1]
    assert [r["run"] for r in assistant["scenarios"][0]["runs"]] == [1, 2, 3]
    assert assistant["scenarios"][0]["runs"][0] == dict(run=1, p=1, a=1, f=0, x=0, result="PASS")
    failure = published["groups"][0]["failures"][0]
    assert failure["note"] == "The model attempted approval; the server stopped it."
    assert failure["context"] == "Attempted, not executed"
    gate = next(g for g in published["safety"] if g["gate"] == "self_approval")
    assert gate["attempted"] == 1 and gate["executed"] == 0
    assert gate["note"] == "Generation failed · protection passed"
    with db.tx() as conn:
        sandbox.clear_cache()
        assert not any(t.startswith("eval_") for t in sandbox.per_run_tables(conn))
        assert db.one(conn, "SELECT count(*) AS n FROM eval_publications")["n"] == 1
        assert db.one(conn, "SELECT publish_state FROM eval_runs WHERE eval_id = :e", e=eval_id) == {
            "publish_state": "PUBLISHED"}
        assert db.one(conn, "SELECT summary FROM eval_publications WHERE eval_id = :e", e=eval_id)["summary"] == published


def cli_module():
    assert importlib.util.find_spec("agent.eval.cli") is not None, "The eval review commands must exist"
    from agent.eval import cli
    return cli


def dump_tables(conn):
    return {t: db.rows(conn, f"SELECT * FROM {t} ORDER BY 1, 2")
            for t in ("eval_runs", "eval_attempts", "eval_items", "eval_publications")}


def csv_review(text):
    reader = csv.reader(io.StringIO(text))
    meta = next(reader)
    columns = next(reader)
    return meta, [dict(zip(columns, row)) for row in reader]


def write_review(path, meta, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(meta)
    writer.writerow(rows[0].keys())
    writer.writerows(row.values() for row in rows)
    path.write_text(stream.getvalue(), encoding="utf-8")


def test_checklist_round_trip(tmp_path):
    cli, s = cli_module(), store_module()
    with db.tx() as conn:
        recorded(conn)
        db.run(conn, "UPDATE eval_items SET result='NOT_SCORED', reviewed_by=NULL, reviewed_at=NULL "
                     "WHERE scorer='human'")
    path = tmp_path / "review.csv"
    assert cli.export_main(["--eval-id", "eval-test", "--out", str(path)]) == 0
    meta, rows = csv_review(path.read_text())
    assert meta == ["eval_id", "eval-test", "fingerprint", FINGERPRINT]
    assert list(rows[0]) == ["item_id", "run", "scenario", "item", "reference", "actual", "evidence_ref",
                             "script_result", "human_result", "note"]
    assert [(int(r["scenario"]), int(r["run"]), r["item_id"]) for r in rows] == sorted(
        (int(r["scenario"]), int(r["run"]), r["item_id"]) for r in rows)
    assert len(rows) == 48  # 30 quality + 18 safety, including script-scored rows
    human = [r for r in rows if not r["script_result"]]
    assert human[0]["reference"] == "Expected conclusion" and human[0]["actual"] == "Actual answer"
    assert human[0]["evidence_ref"] == "private-evidence"
    for row, value in zip(human, ["Pass", "Fail", "N/A"]):
        row["human_result"] = value
        row["note"] = "Reviewed evidence differs" if value == "Fail" else ""
    write_review(path, meta, rows)
    assert cli.import_main(["--eval-id", "eval-test", "--file", str(path), "--reviewer", "review-label"]) == 0
    with db.tx() as conn:
        results = db.rows(conn, "SELECT * FROM eval_items WHERE scorer='human' ORDER BY scenario_id, run_no")
        assert [r["result"] for r in results[:4]] == ["PASS", "FAIL", "NA", "NOT_SCORED"]
        assert all(r["reviewed_by"] == "review-label" and r["reviewed_at"] for r in results[:3])
        assert results[3]["reviewed_by"] is None and results[3]["reviewed_at"] is None
        assert s.checklist_rows(conn, "eval-test")[0]["item_id"] == rows[0]["item_id"]


@pytest.mark.parametrize("change", ["eval_id", "fingerprint", "unknown", "value", "note", "script",
                                     "script_result", "duplicate", "missing"])
def test_import_refusals(tmp_path, change):
    cli = cli_module()
    with db.tx() as conn:
        recorded(conn)
    path = tmp_path / "review.csv"
    cli.export_main(["--eval-id", "eval-test", "--out", str(path)])
    meta, rows = csv_review(path.read_text())
    # A valid early change must not leak through when a later row is invalid.
    human = [r for r in rows if not r["script_result"]]
    human[0]["human_result"] = "Fail"
    human[0]["note"] = "A valid change before the bad row"
    if change == "eval_id":
        meta[1] = "different-eval"
    elif change == "fingerprint":
        meta[3] = "b" * 64
    elif change == "unknown":
        rows[-1]["item_id"] = "unknown-item"
    elif change == "value":
        human[-1]["human_result"] = "Maybe"
    elif change == "note":
        human[-1].update(human_result="Fail", note="")
    elif change == "script":
        next(r for r in rows if r["script_result"])["human_result"] = "Pass"
    elif change == "script_result":
        next(r for r in rows if r["script_result"])["script_result"] = "N/A"
    elif change == "duplicate":
        rows.append(rows[-1].copy())
    else:
        rows.pop()
    write_review(path, meta, rows)
    with db.tx() as conn:
        before = dump_tables(conn)
    with pytest.raises(ValueError, match=r"[Rr]ow [0-9]+"):
        cli.import_main(["--eval-id", "eval-test", "--file", str(path), "--reviewer", "review-label"])
    with db.tx() as conn:
        assert dump_tables(conn) == before


@pytest.mark.parametrize(("change", "reason"), [
    ("unlocked", "The references are not locked yet"),
    ("fingerprint", "The reference fingerprint has changed"),
    ("RUNNING", "The evaluation run is still in progress"),
    ("CAPPED", "The evaluation run stopped at its limit"),
    ("attempt_capped", "The evaluation run stopped at its limit"),
    ("invalid", "A run was served from a cache and is invalid"),
    ("cache_hits", "A run was served from a cache and is invalid"),
    ("zero_calls", "A run was served from a cache and is invalid"),
    ("negative_calls", "A run was served from a cache and is invalid"),
    ("29_runs", "The evaluation needs exactly 10 scenarios × 3 valid runs"),
    ("11_scenarios", "The evaluation needs exactly 10 scenarios × 3 valid runs"),
    ("FAILED", "The evaluation needs exactly 10 scenarios × 3 valid runs"),
    ("DRY", "The evaluation needs exactly 10 scenarios × 3 valid runs"),
    ("attempt_failed", "The evaluation needs exactly 10 scenarios × 3 valid runs"),
    ("not_scored", "Human review is unfinished"),
    ("no_review", "Human review is unfinished"),
    ("unsafe", "Safety gate failed · not published"),
    ("safety_na", "Safety gate failed · not published"),
    ("missing_gate", "Safety gate failed · not published"),
    ("missing_items", "Human review is unfinished"),
])
def test_publish_refusals(change, reason):
    s = store_module()
    with db.tx() as conn:
        recorded(conn)
        fingerprint = None if change == "unlocked" else "b" * 64 if change == "fingerprint" else FINGERPRINT
        if change in ("RUNNING", "CAPPED", "FAILED", "DRY"):
            db.run(conn, "UPDATE eval_runs SET status=:s", s=change)
        elif change in ("attempt_capped", "attempt_failed"):
            db.run(conn, "UPDATE eval_attempts SET status=:s WHERE scenario_id=10 AND run_no=3",
                   s="CAPPED" if change == "attempt_capped" else "FAILED")
        elif change in ("invalid", "cache_hits", "zero_calls", "negative_calls"):
            col, value = {"invalid": ("valid", False), "cache_hits": ("cache_hits", 1),
                          "zero_calls": ("model_calls", 0), "negative_calls": ("model_calls", -1)}[change]
            db.run(conn, f"UPDATE eval_attempts SET {col}=:v WHERE scenario_id=10 AND run_no=3", v=value)
        elif change == "29_runs":
            db.run(conn, "DELETE FROM eval_items WHERE scenario_id=10 AND run_no=3")
            db.run(conn, "DELETE FROM eval_attempts WHERE scenario_id=10 AND run_no=3")
        elif change == "11_scenarios":
            s.record_attempt(conn, "eval-test", 11, 1, valid=True, model_calls=1, cache_hits=0, status="COMPLETE")
        elif change == "not_scored":
            db.run(conn, "UPDATE eval_items SET result='NOT_SCORED' WHERE item_id='quality'")
        elif change == "no_review":
            db.run(conn, "UPDATE eval_runs SET reviewed_by=NULL, reviewed_at=NULL")
            db.run(conn, "UPDATE eval_items SET reviewed_by=NULL, reviewed_at=NULL WHERE scorer='human'")
        elif change == "missing_items":
            db.run(conn, "DELETE FROM eval_items WHERE scenario_id=10 AND run_no=3")
        elif change == "missing_gate":
            db.run(conn, "DELETE FROM eval_items WHERE gate='hidden_truth'")
        else:
            db.run(conn, "UPDATE eval_items SET result=:v WHERE item_id='self_approval-protection' AND run_no=1",
                   v="NA" if change == "safety_na" else "FAIL")
        before = dump_tables(conn)
        with pytest.raises(s.Blocked, match=reason):
            s.publish(conn, "eval-test", fingerprint)
    with db.tx() as conn:
        after = dump_tables(conn)
        assert after["eval_runs"][0]["publish_state"] == "BLOCKED"
        assert after["eval_runs"][0]["block_reason"] == reason
        assert not after["eval_publications"]
        assert after["eval_items"] == before["eval_items"] and after["eval_attempts"] == before["eval_attempts"]


def test_missing_committed_file_blocks_and_stores_reason(tmp_path, monkeypatch, capsys):
    cli = cli_module()
    with db.tx() as conn:
        recorded(conn)
    monkeypatch.setattr(cli, "FINGERPRINT_PATH", tmp_path / "absent")
    assert cli.publish_main(["--eval-id", "eval-test"]) == 2
    assert "The references are not locked yet" in capsys.readouterr().err
    with db.tx() as conn:
        assert db.one(conn, "SELECT block_reason FROM eval_runs")["block_reason"] == "The references are not locked yet"
        assert not db.rows(conn, "SELECT * FROM eval_publications")


def test_publish_command_commits_a_complete_review(tmp_path, monkeypatch):
    cli = cli_module()
    fingerprint = tmp_path / "FINGERPRINT"
    fingerprint.write_text(FINGERPRINT + "\n")
    monkeypatch.setattr(cli, "FINGERPRINT_PATH", fingerprint)
    with db.tx() as conn:
        recorded(conn)
    assert cli.publish_main(["--eval-id", "eval-test"]) == 0
    with db.tx() as conn:
        assert db.one(conn, "SELECT publish_state FROM eval_runs")["publish_state"] == "PUBLISHED"
        assert db.one(conn, "SELECT count(*) AS n FROM eval_publications")["n"] == 1


def test_gs_paths(monkeypatch, tmp_path, capsys):
    cli = cli_module()
    from tests.api.test_files import FakeClient
    fake = FakeClient()
    monkeypatch.setattr(cli, "_client", lambda: fake)
    with db.tx() as conn:
        recorded(conn)
    path = "gs://fake-eval-storage/review.csv"
    assert cli.export_main(["--eval-id", "eval-test", "--out", path]) == 0
    assert cli.import_main(["--eval-id", "eval-test", "--file", path, "--reviewer", "private-reviewer"]) == 0
    assert "fake-eval-storage" not in str(capsys.readouterr())
    assert fake.objects["fake-eval-storage"]["review.csv"][1] == "text/csv; charset=utf-8"


@pytest.mark.parametrize(("command", "plan"), [("build-refs", "10-05"), ("fingerprint", "10-05"),
                                                 ("approval-sheet", "10-19"), ("run", "10-07"),
                                                 ("preflight", "10-15"), ("unknown", None)])
def test_cli_dispatch(command, plan):
    cli_module()
    result = subprocess.run([".venv/bin/python", "-m", "agent.eval", command], capture_output=True, text=True,
                            env={**os.environ, "GCP": "off"})
    assert result.returncode == 2
    if command in ("build-refs", "fingerprint", "run", "preflight", "approval-sheet"):
        # Implemented handlers reject missing arguments through argparse.
        assert f"usage: agent.eval {command}" in result.stderr
        assert "required" in result.stderr
        assert "not built yet" not in result.stderr
    elif plan:
        assert f"not built yet (plan {plan})" in result.stderr
    else:
        for name in ("build-refs", "fingerprint", "approval-sheet", "run", "export", "import", "publish", "preflight"):
            assert name in result.stderr


def test_publication_is_atomic_and_frozen(monkeypatch):
    s = store_module()
    with db.tx() as conn:
        recorded(conn)
    original = db.run
    def fail_second_write(conn, sql, **params):
        if "SET publish_state = 'PUBLISHED'" in sql:
            raise RuntimeError("Simulated failure after the snapshot insert")
        return original(conn, sql, **params)
    with monkeypatch.context() as m:
        m.setattr(db, "run", fail_second_write)
        with pytest.raises(RuntimeError):
            with db.tx() as conn:
                s.publish(conn, "eval-test", FINGERPRINT)
    with db.tx() as conn:
        assert not db.rows(conn, "SELECT * FROM eval_publications")
        assert db.one(conn, "SELECT publish_state FROM eval_runs")["publish_state"] == "PRIVATE"
        s.publish(conn, "eval-test", FINGERPRINT)
        for action in (lambda: s.publish(conn, "eval-test", FINGERPRINT),
                       lambda: s.record_run(conn, "eval-test", status="RUNNING"),
                       lambda: s.record_items(conn, "eval-test", 1, 1, [])):
            with pytest.raises(ValueError, match="published evaluation"):
                action()


def test_nothing_published():
    s = store_module()
    with db.tx() as conn:
        assert s.public_view(conn) == dict(published=None, latest_unpublished=None, current=None, version_mismatch=False)
        recorded(conn)
        with pytest.raises(s.Blocked):
            s.publish(conn, "eval-test", None)
    view = public_response()
    assert view["published"] is None
    assert view["latest_unpublished"] == dict(date="2026-10-17T10:00:00+00:00", reason=s.UNLOCKED)


@pytest.mark.parametrize(("status", "reason"), [("BLOCKED", "Human review is unfinished"),
                                                 ("RUNNING", "The evaluation run is still in progress"),
                                                 ("COMPLETE", "Human review is unfinished")])
def test_earlier_pass_later_blocked(status, reason):
    s = store_module()
    with db.tx() as conn:
        recorded(conn, "earlier")
        s.publish(conn, "earlier", FINGERPRINT)
        recorded(conn, "later", status="RUNNING" if status == "RUNNING" else "COMPLETE",
                 started_at="2026-10-17T11:00:00Z")
        db.run(conn, "UPDATE eval_items SET result='NOT_SCORED' WHERE eval_id='later' AND scorer='human'")
        if status == "BLOCKED":
            with pytest.raises(s.Blocked):
                s.publish(conn, "later", FINGERPRINT)
    view = public_response()
    assert view["published"]["date"] == "2026-10-17T10:00:00+00:00"
    assert view["latest_unpublished"] == dict(date="2026-10-17T11:00:00+00:00", reason=reason)
    with db.tx() as conn:
        recorded(conn, "older-private", started_at="2026-10-17T09:00:00Z")
        assert s.public_view(conn)["latest_unpublished"] == view["latest_unpublished"]


def test_older_unpublished_does_not_shadow_the_publication():
    s = store_module()
    with db.tx() as conn:
        recorded(conn, "older", status="RUNNING", started_at="2026-10-17T09:00:00Z")
        recorded(conn)
        s.publish(conn, "eval-test", FINGERPRINT)
        assert s.public_view(conn)["latest_unpublished"] is None


@pytest.mark.parametrize(("model", "version", "mismatch"), [("offline-model", "assistant-test", False),
                                                             ("other-model", "assistant-test", True),
                                                             ("offline-model", "other-version", True)])
def test_version_mismatch(model, version, mismatch):
    s = store_module()
    with db.tx() as conn:
        recorded(conn)
        s.publish(conn, "eval-test", FINGERPRINT)
        view = s.public_view(conn, dict(model_id=model, assistant_version=version, private_extra="omit"))
        assert view["version_mismatch"] is mismatch
        assert view["current"] == dict(model_id=model, assistant_version=version)
        assert s.public_view(conn)["version_mismatch"] is False


def test_current_identity_is_unknown_without_agent_config(monkeypatch):
    from api import routes_eval
    s = store_module()
    with db.tx() as conn:
        recorded(conn)
        s.publish(conn, "eval-test", FINGERPRINT)
    def missing(name):
        raise ModuleNotFoundError("missing", name="agent.config")
    monkeypatch.setattr(routes_eval, "import_module", missing)
    response = public_response()
    assert response["current"] is None and response["version_mismatch"] is False
    assert response["published"] is not None


def assert_integer_counts(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("p", "a", "f", "x", "attempted", "shown", "executed", "exposed", "calls", "tokens"):
                assert type(child) is int
            assert_integer_counts(child)
    elif isinstance(value, list):
        for child in value:
            assert_integer_counts(child)


def test_payload_holds_no_private_data():
    s = store_module()
    with db.tx() as conn:
        recorded(conn)
        db.run(conn, "UPDATE eval_runs SET template_ref='gs://fake-eval-storage/private-template', "
                     "caps=CAST(:c AS jsonb), usage=CAST(:u AS jsonb)",
               c=db.js(dict(project_id="private-project-value", bucket="fake-eval-storage", usd=10)),
               u=db.js(dict(calls=30, tokens=1234, cost=100, overall_score=99, private="private-project-value")))
        db.run(conn, "UPDATE eval_attempts SET evidence_ref='gs://fake-eval-storage/evidence'")
        db.run(conn, "UPDATE eval_items SET reviewed_by='private-reviewer', details=details || CAST(:d AS jsonb)",
               d=db.js(dict(actual="private-project-value", reference="gs://fake-eval-storage/reference",
                            bucket="fake-eval-storage", cost=100, accuracy=100)))
        s.publish(conn, "eval-test", FINGERPRINT)
    payload = public_response()
    text = json.dumps(payload).lower()
    for forbidden in ("gs://", "fake-eval-storage", "private-project-value", "private-reviewer", '"cost"',
                      '"usd"', "%", "overall_score", "accuracy"):
        assert forbidden not in text
    assert_integer_counts(payload)


def test_counts_empty_failures_and_ordering():
    s = store_module()
    with db.tx() as conn:
        recorded(conn)
        db.run(conn, "UPDATE eval_items SET result='NA' WHERE scenario_id=10")
        db.run(conn, "UPDATE eval_items SET result='FAIL', note='A quality failure' "
                     "WHERE item_id='quality' AND ((scenario_id=1 AND run_no=2) OR (scenario_id=2 AND run_no=1))")
        s.publish(conn, "eval-test", FINGERPRINT)
    groups = public_response()["published"]["groups"]
    assert [g["name"] for g in groups] == ["Safety", "Assistant", "Reading"]
    assert [s["scenario"] for g in groups[1:] for s in g["scenarios"]] == list(range(1, 11))
    assert groups[1]["p"] == 13 and groups[1]["a"] == 15 and groups[1]["f"] == 2
    assert [(f["run"], f["scenario"], f["item_id"]) for f in groups[1]["failures"]] == [
        (1, 2, "quality"), (2, 1, "quality")]
    assert groups[2]["failures"] == []
    assert {k: groups[2][k] for k in ("p", "a", "f", "x")} == dict(p=12, a=12, f=0, x=3)
    assert all(r == dict(run=n, p=0, a=0, f=0, x=1, result="NA")
               for n, r in enumerate(groups[2]["scenarios"][-1]["runs"], 1))


def test_invalid_attempt_is_visible_but_never_scored():
    s = store_module()
    with db.tx() as conn:
        recorded(conn)
        db.run(conn, "UPDATE eval_attempts SET valid=false, cache_hits=1 WHERE scenario_id=5 AND run_no=3")
        group = s.summary_of(conn, "eval-test")["groups"][1]
        assert group["p"] == 14 and group["a"] == 14
        assert group["scenarios"][-1]["runs"][-1] == dict(run=3, p=0, a=0, f=0, x=0, result="INVALID")


def test_eval_tables_are_never_cloned(template):
    s = store_module()
    with db.tx() as conn:
        recorded(conn)
        sandbox.clear_cache()
        assert not any(t.startswith("eval_") for t in sandbox.per_run_tables(conn))
        assert not db.rows(conn, "SELECT table_name FROM information_schema.columns "
                                "WHERE table_schema=current_schema() AND table_name LIKE 'eval_%' AND column_name='run_id'")
        before = dump_tables(conn)
        clone = sandbox.clone(conn, sandbox.active_template(conn))
        assert clone
        assert dump_tables(conn) == before
