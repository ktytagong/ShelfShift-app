"""coerce_input and validate_input against their f694682 versions on real stored and screened inputs (quick 261017-tps).

The oracle is the verbatim copy in tests/engine/test_input_checks_identity.py. Here the corpus is the synthetic
template's stored recommendation inputs (their rec_version and cache key must not change) and the raw inputs the store
rule's screen sees on a morning: the running extra Bookings of the transfers it made earlier that morning ride in
every later lot's input. That morning runs inside a rolled-back transaction, so the template is left as it was.
"""
from __future__ import annotations

import engine.canonical
from api import advance, db, evaluate, screen
from engine import from_dict, rec_version
from engine.adjust import coerce_input, validate_input
from tests.engine.test_input_checks_identity import canon, old_coerce_input, old_validate_input, outcome


class _Rollback(Exception):
    pass


def _old_checked(x):
    y = old_coerce_input(x)
    old_validate_input(y)
    return y


def _identical(x) -> None:
    from engine.adjust import checked_input
    assert outcome(coerce_input, x) == outcome(old_coerce_input, x)
    assert outcome(validate_input, x) == outcome(old_validate_input, x)
    assert outcome(checked_input, x) == outcome(_old_checked, x)


def test_stored_template_inputs_are_identical_under_the_oracle(template, monkeypatch):
    with db.tx() as conn:
        recs = db.rows(conn, "SELECT rec_version, engine_input FROM recommendations WHERE run_id = :r "
                             "ORDER BY case_id, rec_version", r=template)
    assert recs
    inputs = [from_dict(r["engine_input"]) for r in recs]
    for rec, x in zip(recs, inputs):
        _identical(x)
        assert rec_version(x) == rec["rec_version"]
    new = [(rec_version(x), evaluate.cache_key(x)) for x in inputs]
    monkeypatch.setattr(engine.canonical, "coerce_input", old_coerce_input)
    assert [(rec_version(x), evaluate.cache_key(x)) for x in inputs] == new


def _record_morning(run_id: str, monkeypatch) -> list:
    """The store rule's next morning on this template, rolled back: every raw input its screen was given."""
    seen = []
    original = screen.screen

    def recording(raw_inp, thresholds):
        seen.append(raw_inp)
        return original(raw_inp, thresholds)

    monkeypatch.setattr(screen, "screen", recording)
    with db.tx() as conn:
        before = db.one(conn, "SELECT count(*) AS n FROM world_actions WHERE run_id = :r", r=run_id)["n"]
    try:
        with db.tx() as conn:
            advance.baseline_morning(conn, db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id))
            raise _Rollback
    except _Rollback:
        pass
    monkeypatch.setattr(screen, "screen", original)
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM world_actions WHERE run_id = :r", r=run_id)["n"] == before
    return seen


def _running(x) -> bool:
    b = x.bookings
    return bool(b.totes or b.run_totes or b.lot_units)


def test_store_rule_screen_inputs_are_identical_under_the_oracle(template, monkeypatch, request):
    seen = _record_morning(template, monkeypatch)
    assert seen
    for x in seen:
        _identical(x)
    running = sum(map(_running, seen))
    if not running:  # the synthetic morning made no transfer: the tiny public template's morning, the same way
        public = request.getfixturevalue("public_template")
        seen = _record_morning(public, monkeypatch)
        assert seen
        for x in seen:
            _identical(x)
        running = sum(map(_running, seen))
    print(f"store-rule screen inputs: {len(seen)}, with running rows: {running}")
