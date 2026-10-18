"""A failed release goes back to the known-good template, and no visitor's sandbox changes (06-CONTEXT D-17; OPS-05).

The release (06-07) builds a candidate under a run id it chose (`build --run-id`), reads the template that activation
retired from the `retired:` line, and on any later failure (the build job itself after promotion, the bench, the
deploy, the smoke or the journey) runs `restore <new>`. These tests force those failures on the local database:
restore puts the predecessor back in one transaction, a restore of a template that is not ACTIVE does nothing (so
the release can run it after every failed or uncertain outcome; 06-REVIEWS R2-1), and the sandboxes visitors
opened during the window keep serving their case (R2-10, D-R4). The session's template is ACTIVE again after every
test, so later tests are unaffected.

The deploy job also grants the runtime database user its table and sequence privileges at every build (research
Pitfall 5), with the role name validated before any SQL runs (R2-9).
"""
import json
import uuid
from contextlib import contextmanager

import pytest
import sqlalchemy as sa

from api import cases, config, db, sandbox, views, workflow
from api.errors import ApiError
from tests.api.conftest import fresh, seat, take

STORY = cases.STORY_KEY
NEW, PRUNE, PROMOTED, WINDOW, NOTHING = ("tpl-p6-test-new", "tpl-p6-test-prune", "tpl-p6-test-promoted",
                                         "tpl-p6-test-window", "tpl-p6-test-nothing")
FIRST, SECOND = "tpl-p6-test-first", "tpl-p6-test-second"


@pytest.fixture(autouse=True)
def known_good_template_back(template):
    """After each test, the session's template is the one ACTIVE TEMPLATE again, whatever the test left."""
    yield
    with db.tx() as conn:
        db.run(conn, "UPDATE demo_runs SET status = 'RETIRED', retired_at = coalesce(retired_at, now()) "
                     "WHERE kind = 'TEMPLATE' AND status = 'ACTIVE' AND run_id <> :t", t=template)
        db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE', retired_at = NULL WHERE run_id = :t", t=template)


def active_id() -> str | None:
    with db.tx() as conn:
        t = sandbox.active_template(conn)
    return t["run_id"] if t else None


def new_clone() -> str:
    """A visitor's new sandbox, cloned from whatever template is ACTIVE now."""
    with db.tx() as conn:
        return sandbox.clone(conn, sandbox.active_template(conn))


def runs_dump() -> list[str]:
    """Every demo_runs row, exactly as stored."""
    with db.tx() as conn:
        return [r["j"] for r in db.rows(conn, "SELECT row_to_json(d)::text AS j FROM demo_runs d ORDER BY run_id")]


def snapshot(run_id: str) -> dict[str, list[str]]:
    """Every per-run row of a sandbox and its demo_runs row, as stored text, in a fixed order."""
    with db.tx() as conn:
        out = {t: [r["j"] for r in db.rows(conn, f"SELECT row_to_json(x)::text AS j FROM {t} x WHERE run_id = :r "
                                                 "ORDER BY 1", r=run_id)]
               for t in sandbox.per_run_tables(conn)}
        out["demo_runs"] = [db.one(conn, "SELECT row_to_json(d)::text AS j FROM demo_runs d WHERE run_id = :r",
                                   r=run_id)["j"]]
    return out


def send_story(run_id: str) -> None:
    """SUS-19 sent for approval by its source seat, as a visitor would (the case then holds its bookings)."""
    with db.tx() as conn:
        case = cases.case_row(conn, run_id, STORY)
    take(run_id, seat(case["source_branch"]), STORY)  # only the owner sends (06.1 D-54, D-67)
    sent = db.in_tx(lambda conn: workflow.send(conn, db.lock_run(conn, run_id), seat(case["source_branch"]), STORY,
                                               case["active_rec_version"]))
    assert sent["state"] == "PENDING_APPROVAL"


def build(run_id: str) -> int:
    return sandbox.main(["build", "--allow-local", "--run-id", run_id])


@pytest.mark.parametrize("extra", [[], ["--first"]], ids=["plain", "first"])
def test_a_failure_after_the_build_goes_back_to_the_known_good_template(template, capsys, extra):
    """With a predecessor, --first is the plain restore: the same lines and the same rows (261017-dso WR-06)."""
    new = NEW + ("-first" if extra else "")  # each case builds its own candidate
    t1 = active_id()
    assert t1 == template
    s1 = new_clone()
    send_story(s1)
    before = snapshot(s1)
    capsys.readouterr()

    assert build(new) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"previous ACTIVE: {t1}"
    i = out.index(f"template {new} ACTIVE for {fresh(t1)['dataset_version']}")
    assert out[i + 1] == f"retired: {t1}"  # the template this activation retired, from its retired_at stamp (R2-11)
    assert out[i + 2].startswith("pruned ")
    assert fresh(new)["status"] == "ACTIVE"
    old = fresh(t1)
    assert old["status"] == "RETIRED" and old["retired_at"] is not None  # step 6 stamps the retired template

    assert sandbox.main(["restore", new, *extra]) == 0
    assert capsys.readouterr().out.splitlines() == [f"restored: {t1} ACTIVE, {new} RETIRED", f"ACTIVE: {t1}"]
    assert active_id() == t1 and fresh(t1)["retired_at"] is None
    assert fresh(new_clone())["template_run_id"] == t1  # new visitors get the known-good template
    assert snapshot(s1) == before  # the existing sandbox is byte-identical and still ACTIVE
    assert fresh(s1)["status"] == "ACTIVE"
    kept = fresh(new)
    assert kept["status"] == "RETIRED" and kept["retired_at"] is not None  # kept, for the post-mortem

    dump = runs_dump()
    assert sandbox.main(["restore", new, *extra]) == 0  # repeated after it succeeded: a no-op
    assert capsys.readouterr().out.splitlines() == [f"restore: {new} is not ACTIVE; nothing to do", f"ACTIVE: {t1}"]
    assert sandbox.main(["restore", "tpl-nope", *extra]) == 0  # unknown: a no-op
    assert capsys.readouterr().out.splitlines() == ["restore: tpl-nope is not ACTIVE; nothing to do", f"ACTIVE: {t1}"]
    assert runs_dump() == dump


@pytest.mark.parametrize("extra", [[], ["--first"]], ids=["plain", "first"])
@pytest.mark.parametrize("status", ["FAILED", "INITIALIZING"])
def test_a_restore_of_a_template_that_is_not_active_does_nothing(template, status, capsys, extra):
    rid = f"tpl-p6-test-{status.lower()}"
    t = fresh(template)
    with db.tx() as conn:
        db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) VALUES "
                     "(:r, 'TEMPLATE', :v, :s, 0, :n)", r=rid, v=t["dataset_version"], s=status, n=t["next_case_no"])
    try:
        dump = runs_dump()
        assert sandbox.main(["restore", rid, *extra]) == 0
        assert capsys.readouterr().out.splitlines() == [f"restore: {rid} is not ACTIVE; nothing to do",
                                                        f"ACTIVE: {template}"]
        assert runs_dump() == dump
        assert active_id() == template
    finally:
        with db.tx() as conn:
            db.run(conn, "DELETE FROM demo_runs WHERE run_id = :r", r=rid)


@pytest.mark.parametrize("broken, rid", [("prune", PRUNE), ("predecessor", PROMOTED)])
def test_a_failure_after_promotion_is_undone_by_restore(template, monkeypatch, capsys, broken, rid):
    """The build job fails after step 6 (right after promotion, or while pruning): it exits non-zero with the new
    template ACTIVE, and the release's restore puts the previous one back (06-REVIEWS R2-1)."""
    t1 = active_id()

    def breaks(*args, **kwargs):
        raise RuntimeError(f"{broken} broke")

    monkeypatch.setattr(sandbox, broken, breaks)
    try:
        code = build(rid)
    except RuntimeError as exc:
        assert f"{broken} broke" in str(exc)
    else:
        assert code != 0
    monkeypatch.undo()
    out = capsys.readouterr().out
    assert ("retired:" in out) == (broken == "prune")
    assert fresh(rid)["status"] == "ACTIVE" and fresh(t1)["status"] == "RETIRED"  # promoted before the failure

    assert sandbox.main(["restore", rid]) == 0
    assert active_id() == t1
    assert fresh(rid)["status"] == "RETIRED"


def test_two_failed_releases_in_a_row_restore_the_original_template(template, monkeypatch, capsys):
    """Two failed releases in a row (06-REVIEW CR-01, CR-02): each restore names the original template ACTIVE, and
    the second build's activation retired the original, never the first failed one."""
    t1 = active_id()
    assert t1 == template
    capsys.readouterr()

    assert build(FIRST) == 0
    assert f"retired: {t1}" in capsys.readouterr().out.splitlines()
    assert sandbox.main(["restore", FIRST]) == 0
    assert capsys.readouterr().out.splitlines() == [f"restored: {t1} ACTIVE, {FIRST} RETIRED", f"ACTIVE: {t1}"]

    def breaks(*args, **kwargs):
        raise RuntimeError("prune broke")

    monkeypatch.setattr(sandbox, "prune", breaks)  # the second release's build fails after promotion
    with pytest.raises(RuntimeError, match="prune broke"):
        build(SECOND)
    monkeypatch.undo()
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"previous ACTIVE: {t1}"
    assert f"retired: {t1}" in out  # the original, never FIRST

    assert sandbox.main(["restore", SECOND]) == 0
    assert capsys.readouterr().out.splitlines() == [f"restored: {t1} ACTIVE, {SECOND} RETIRED", f"ACTIVE: {t1}"]
    assert sandbox.main(["restore", SECOND]) == 0  # the release retrying a recovery
    assert capsys.readouterr().out.splitlines() == [f"restore: {SECOND} is not ACTIVE; nothing to do", f"ACTIVE: {t1}"]
    assert active_id() == t1
    assert fresh(FIRST)["status"] == "RETIRED" and fresh(SECOND)["status"] == "RETIRED"


def test_sandboxes_opened_during_a_release_window_survive_the_restore(template, capsys):
    t1 = active_id()
    s1 = new_clone()  # opened before the promotion
    assert build(WINDOW) == 0
    s2, s3 = new_clone(), new_clone()  # visitors arriving while the new template is ACTIVE
    assert fresh(s2)["template_run_id"] == WINDOW

    assert sandbox.main(["restore", WINDOW]) == 0
    assert active_id() == t1
    for sid in (s1, s2):
        run = fresh(sid)
        assert run["status"] == "ACTIVE", sid
        with db.tx() as conn:
            assert STORY in [c["key"] for c in cases.board(conn, run)], sid
            d = cases.detail(conn, run, STORY)
        assert d["key"] == STORY and d["result"] is not None and d["versions"]["rec_version"], sid
    assert fresh(s2)["template_run_id"] == WINDOW and fresh(WINDOW)["status"] == "RETIRED"

    # S3 stands in for a sandbox from a template the serving image cannot read: export fails loudly, never computes
    with db.tx() as conn:
        case = cases.case_row(conn, s3, STORY)
        assert db.run(conn, "UPDATE recommendations SET engine_input = jsonb_set(engine_input, '{engine_version}', "
                            "CAST(:x AS jsonb)) WHERE run_id = :r AND case_id = :c AND rec_version = :v",
                      x='"engine-other"', r=s3, c=STORY, v=case["active_rec_version"]) == 1
    with db.tx() as conn:
        with pytest.raises(ApiError) as e:
            views.export(conn, fresh(s3), STORY)
    assert (e.value.status, e.value.code) == (409, "engine_version")


def test_the_build_prints_retired_none_when_it_retired_nothing(template, capsys):
    """With no ACTIVE template at activation, the line says none, even though older stamped templates exist (an
    activation never claims a template something else retired; R2-11)."""
    with db.tx() as conn:
        db.run(conn, "UPDATE demo_runs SET status = 'RETIRED', retired_at = NULL WHERE run_id = :t", t=template)
    assert build(NOTHING) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "previous ACTIVE: none"
    i = out.index(f"template {NOTHING} ACTIVE for {fresh(template)['dataset_version']}")
    assert out[i + 1] == "retired: none"
    with db.tx() as conn:
        assert sandbox.predecessor(conn, NOTHING) is None


def test_a_restore_with_no_older_template_exits_1_and_changes_nothing(template, capsys):
    assert active_id() == template
    dump = runs_dump()
    assert sandbox.main(["restore", template]) == 1
    assert capsys.readouterr().err.strip() == f"restore failed: no previous template to restore before {template}"
    assert runs_dump() == dump
    assert active_id() == template


def test_restore_first_retires_a_template_that_retired_nothing(template, capsys):
    """A failed first release (06-REVIEW WR-06): --first retires an ACTIVE template whose activation retired none, so
    no template is ACTIVE, the state before that release; nothing else changes."""
    assert active_id() == template
    with db.tx() as conn:
        assert sandbox.predecessor(conn, template) is None
    others = [r for r in runs_dump() if json.loads(r)["run_id"] != template]
    assert sandbox.main(["restore", template, "--first"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"first release undone: {template} RETIRED", "ACTIVE: none"]
    assert active_id() is None
    row = fresh(template)
    assert row["status"] == "RETIRED" and row["retired_at"] is not None
    assert [r for r in runs_dump() if json.loads(r)["run_id"] != template] == others


def test_has_template_table_reads_the_real_schema():
    assert db.in_tx(sandbox.has_template_table) is True
    assert db.in_tx(sandbox.has_template_table, "no_such_table_p6") is False


def test_restore_and_active_read_a_missing_template_table_as_none(template, monkeypatch, capsys):
    """A fresh database whose first build failed at migrate has no demo_runs table (06-REVIEW WR-06): restore and
    active print `ACTIVE: none` and exit 0, and touch nothing."""
    def refuse(*args, **kwargs):
        raise AssertionError("no template query may run when the template table is missing")

    monkeypatch.setattr(sandbox, "has_template_table", lambda conn, table="demo_runs": False)
    monkeypatch.setattr(sandbox, "restore", refuse)
    monkeypatch.setattr(sandbox, "active_template", refuse)
    capsys.readouterr()
    dump = runs_dump()
    for extra in ([], ["--first"]):
        assert sandbox.main(["restore", template, *extra]) == 0
        assert capsys.readouterr().out.splitlines() == [f"restore: {template} is not ACTIVE; nothing to do",
                                                        "ACTIVE: none"]
    assert sandbox.main(["active"]) == 0
    assert capsys.readouterr().out.splitlines() == ["ACTIVE: none"]
    assert runs_dump() == dump


@pytest.mark.parametrize("bad", ["Bad ID", "tpl-", "tpl--x", "tpl-UPPER", "sbx-abc", "tpl-" + "a" * 63,
                                 "tpl-a;drop"])
@pytest.mark.parametrize("command", ["build", "restore"])
def test_a_run_id_outside_the_pattern_is_refused_before_any_write(template, capsys, command, bad):
    dump = runs_dump()
    with pytest.raises(SystemExit) as e:
        sandbox.main([command, "--run-id", bad] if command == "build" else [command, bad])
    assert e.value.code == 2
    assert runs_dump() == dump


def test_the_longest_run_id_is_accepted_by_the_pattern(template, capsys):
    longest = "tpl-" + "a" * 62  # 66 characters
    assert sandbox.main(["restore", longest]) == 0  # unknown: accepted by the type check, then a no-op
    assert "nothing to do" in capsys.readouterr().out


def test_a_taken_run_id_is_refused_and_nothing_changes(template, capsys):
    dump = runs_dump()
    assert build(template) == 1
    assert capsys.readouterr().err.strip() == f"template build failed: template run id {template} already exists"
    assert runs_dump() == dump
    assert active_id() == template


def test_the_help_lists_the_run_id(capsys):
    for argv, want in ((["build", "--help"], "--run-id"), (["restore", "--help"], "RUN_ID")):
        with pytest.raises(SystemExit) as e:
            sandbox.main(argv)
        assert e.value.code == 0
        assert want in capsys.readouterr().out


def test_active_prints_the_active_template_and_writes_nothing(template, monkeypatch, capsys):
    """`active` (the release's --status reads it from the build job's log) prints the restore's ACTIVE line and
    nothing else: SELECT statements only, no migrate, no reference install, demo_runs byte-identical."""
    def refuse(*args, **kwargs):
        raise AssertionError("api.sandbox active must not migrate or install a reference set")

    monkeypatch.setattr(sandbox.db, "migrate", refuse)
    monkeypatch.setattr(sandbox.refdata, "install", refuse)
    capsys.readouterr()
    dump = runs_dump()
    with statements_sent() as sent:
        assert sandbox.main(["active"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"ACTIVE: {template}"]
    assert runs_dump() == dump
    assert sent and all(s.strip().upper().startswith("SELECT") for s in sent), sent

    with db.tx() as conn:  # no ACTIVE template (the autouse fixture puts it back)
        db.run(conn, "UPDATE demo_runs SET status = 'RETIRED', retired_at = now() WHERE run_id = :t", t=template)
    assert sandbox.main(["active"]) == 0
    assert capsys.readouterr().out.splitlines() == ["ACTIVE: none"]
    with pytest.raises(SystemExit) as e:
        sandbox.main(["active", "--help"])
    assert e.value.code == 0


# --- the deploy job grants the runtime user at every build (Pitfall 5; 06-REVIEWS R2-9) -------------------------------

TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
SEQUENCE_PRIVILEGES = ("USAGE", "SELECT")


@contextmanager
def statements_sent():
    """Every SQL statement the engine sends while the block runs."""
    sent: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        sent.append(statement)

    sa.event.listen(db.engine(), "before_cursor_execute", record)
    try:
        yield sent
    finally:
        sa.event.remove(db.engine(), "before_cursor_execute", record)


def missing_privileges(role: str) -> list[str]:
    """Each privilege the role lacks, one by one (a comma list in has_table_privilege means any, not all)."""
    with db.tx() as conn:
        tables = [r["t"] for r in db.rows(conn, "SELECT tablename AS t FROM pg_tables "
                                                "WHERE schemaname = current_schema() ORDER BY 1")]
        sequences = [r["s"] for r in db.rows(conn, "SELECT sequencename AS s FROM pg_sequences "
                                                   "WHERE schemaname = current_schema() ORDER BY 1")]
        assert "demo_runs" in tables and "activity_seq" in sequences
        missing = [f"{t} {p}" for t in tables for p in TABLE_PRIVILEGES
                   if not db.one(conn, "SELECT has_table_privilege(:r, quote_ident(:t), :p) AS ok", r=role, t=t,
                                 p=p)["ok"]]
        missing += [f"{s} {p}" for s in sequences for p in SEQUENCE_PRIVILEGES
                    if not db.one(conn, "SELECT has_sequence_privilege(:r, quote_ident(:s), :p) AS ok", r=role, s=s,
                                  p=p)["ok"]]
    return missing


def test_migrate_grants_the_runtime_user_every_table_and_sequence(template, monkeypatch):
    role = f"p6_run_probe_{uuid.uuid4().hex[:10]}"
    with db.tx() as conn:
        conn.execute(sa.text(f'CREATE ROLE "{role}" NOLOGIN'))
    try:
        assert missing_privileges(role)  # a new role starts with nothing
        monkeypatch.setattr(config, "RUN_DB_USER", role)
        db.migrate()
        assert missing_privileges(role) == []
        db.migrate()  # every build grants again: idempotent
        assert missing_privileges(role) == []
    finally:
        with db.tx() as conn:
            conn.execute(sa.text(f'DROP OWNED BY "{role}"'))
            conn.execute(sa.text(f'DROP ROLE "{role}"'))


def test_migrate_without_a_runtime_user_grants_nothing(template, monkeypatch):
    monkeypatch.setattr(config, "RUN_DB_USER", "")
    with statements_sent() as sent:
        db.migrate()
    assert sent  # the schema statements ran
    assert not [s for s in sent if s.lstrip().upper().startswith("GRANT")]


def test_a_missing_runtime_role_fails_the_build_loudly(template, monkeypatch):
    monkeypatch.setattr(config, "RUN_DB_USER", f"p6_no_such_role_{uuid.uuid4().hex[:10]}")
    with pytest.raises(sa.exc.DBAPIError):
        db.migrate()


@pytest.mark.parametrize("bad", ['bad"name', "UPPER", "a b", "a" * 64, "-lead", "x;drop", "p:x"])
def test_a_bad_runtime_role_name_is_refused_before_any_sql(monkeypatch, bad):
    entered = []
    real = db.tx

    @contextmanager
    def spy():
        entered.append(1)
        with real() as conn:
            yield conn

    monkeypatch.setattr(config, "RUN_DB_USER", bad)
    monkeypatch.setattr(db, "tx", spy)
    with pytest.raises(ValueError) as e:
        db.migrate()
    assert "RUN_DB_USER" in str(e.value) and bad not in str(e.value)  # names the setting, never its value
    assert entered == []  # zero SQL calls: the schema statements never ran either
