"""D-09..D-11's equivalent stock checks on the benchmark's own INITIALIZING run (08-13; research Q3, Q4).

Every scenario runs on the TEST ONLY eligible tiny public set (tests/api/benchmark_fixtures.py), opened with
`eager=True` so every qualifying lot is evaluated (D-07). Record errors are made by test-local record changes at the
source (a recorded quantity above the physical one, an old last count), as Phase 7's tests do; no fact is injected,
so every check below is one the engine itself flagged with changes_plan. Counts are answered only from each world's
own physical stock (D-10).
"""
import pytest

from api import benchmark, cases, db, overlay, signals, weekcheck
from api.errors import ApiError
from tests.api.benchmark_fixtures import eligible_dv  # noqa: F401
from tests.api.conftest import drop_runs, seat

RESULT_TABLES = ('benchmark_days', 'benchmark_observations', 'benchmark_branch_day', 'benchmark_summary',
                 'benchmark_transfer_audit', 'evaluation_check_history', 'benchmark_runs')


def make_run(eligible_dv, monkeypatch, shape=None):
    """A bm run whose day-0 stock is reshaped by `shape(conn, run_id)` inside world initialisation (every world
    alike, before the eager morning opens the cases)."""
    from api import sandbox
    if shape is not None:
        initialize = sandbox.init_worlds

        def shaped(conn, run_id, ref, truth, **kwargs):
            initialize(conn, run_id, ref, truth, **kwargs)
            shape(conn, run_id)
        monkeypatch.setattr(sandbox, 'init_worlds', shaped)
    with db.tx() as conn:
        run = benchmark.create_run(conn, eligible_dv, trial=False, git_commit='test-only',
                                   image_digest='test-only', worlds=weekcheck.BENCH_WORLDS)
    monkeypatch.undo()
    return run


def drop(run):
    with db.tx() as conn:
        for table in RESULT_TABLES:
            column = 'sim_run_id' if table in ('benchmark_transfer_audit', 'evaluation_check_history') else 'benchmark_id'
            db.run(conn, f'DELETE FROM {table} WHERE {column} = :r', r=run['run_id'])
        drop_runs(conn, [run['run_id']])


def stale_designed_lot(conn, run_id):
    """The designed lot: 53 on every record, 50 physically, expiring today, last counted long ago; the other stores'
    stock and deliveries of its product are removed (a destination with room), never demand or forecasts."""
    lot = db.one(conn, "SELECT * FROM lots WHERE run_id = :r AND world = 'shelfshift' AND tag = 'designed'", r=run_id)
    db.run(conn, "UPDATE lots SET qty = 53, expiry_day = 0, last_verified_day = -10 WHERE run_id = :r AND lot_id = :l",
           r=run_id, l=lot['lot_id'])
    db.run(conn, "UPDATE truth_lots SET qty = 50 WHERE run_id = :r AND lot_id = :l", r=run_id, l=lot['lot_id'])
    db.run(conn, "DELETE FROM truth_lots WHERE run_id = :r AND lot_id IN (SELECT lot_id FROM lots "
                 "WHERE run_id = :r AND sku = :s AND branch <> :b)", r=run_id, s=lot['sku'], b=lot['branch'])
    db.run(conn, "DELETE FROM lots WHERE run_id = :r AND sku = :s AND branch <> :b",
           r=run_id, s=lot['sku'], b=lot['branch'])
    db.run(conn, "DELETE FROM inbound WHERE run_id = :r AND sku = :s", r=run_id, s=lot['sku'])


@pytest.fixture
def stale(eligible_dv, monkeypatch):
    run = make_run(eligible_dv, monkeypatch, stale_designed_lot)
    yield run
    drop(run)


def count_facts(conn, case):
    rec = cases.active_rec(conn, case)
    from api import commitment
    return rec, [f for f in commitment.checks_in_force(case, rec)
                 if f['changes_plan'] and f['kind'] in signals.COUNT_KINDS]


def designed_case(conn, run):
    lot = db.one(conn, "SELECT * FROM lots WHERE run_id = :r AND world = 'shelfshift' AND tag = 'designed'",
                 r=run['run_id'])
    [case] = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND lot_id = :l AND state = 'OPEN'",
                     r=run['run_id'], l=lot['lot_id'])
    return lot, case


def test_tracer_one_count_corrects_both_worlds_from_their_own_stock(stale):
    with db.tx() as conn:
        lot, case = designed_case(conn, stale)
        rec, facts = count_facts(conn, case)
        fact = next(f for f in facts if f['lot_ids'] == [lot['lot_id']])
        # the comparison world's own physical stock differs from ShelfShift's (D-10: never ShelfShift's quantity)
        db.run(conn, "UPDATE truth_lots SET qty = 44 WHERE run_id = :r AND world = 'baseline_checked' AND lot_id = :l",
               r=stale['run_id'], l=lot['lot_id'])
        q = signals.request_evidence(conn, stale, seat(case['source_branch']), case['case_id'], rec['rec_version'],
                                     fact['fact_id'])
        physical = overlay.physical(conn, stale, 'shelfshift', q['lot_ids'])
        assert physical == {lot['lot_id']: 50.0}
        answer = signals.answer(conn, stale, seat(q['to_branch']), q['request_id'], 'counted',
                                {l: {'shelf_units': physical[l], 'backroom_units': 0.0} for l in q['lot_ids']}, 'now')
        with pytest.raises(ApiError) as refused:  # the default stays ACTIVE: no route can confirm on this run
            signals.confirm(conn, stale, seat(q['to_branch']), answer['signal_id'], answer['revision'], True)
        assert refused.value.code == 'sandbox_reset'
        done = signals.confirm(conn, stale, seat(q['to_branch']), answer['signal_id'], answer['revision'], True,
                               statuses=("INITIALIZING",))
        assert done['fact_id']
        record = lambda world: db.one(conn, 'SELECT qty, verified_by, last_verified_day FROM lots WHERE run_id = :r '
                                            'AND world = :w AND lot_id = :l', r=stale['run_id'], w=world,
                                      l=lot['lot_id'])
        assert record('shelfshift')['qty'] == 50
        assert record('baseline_checked')['qty'] == 53  # untouched until its own count
        corrected = weekcheck.correct_compare_world(conn, stale, q['lot_ids'], stale['day'])
        assert corrected == {lot['lot_id']: {'record_before': 53.0, 'physical': 44.0, 'correction': 'down'}}
        assert dict(record('baseline_checked')) == {'qty': 44, 'verified_by': 'COUNT', 'last_verified_day': 0}
        assert record('baseline_rule')['qty'] == 53  # scenario 1 keeps its original records


# ---- Task 2: the D-11 checked morning ------------------------------------------------------------------------------

def add_lot(conn, run_id, lot_id, record, physical, *, expiry=0, verified=-10, branch=None, worlds=None):
    """A lot of the designed lot's product, on the record and physically, in the named worlds (every benchmark world
    by default), inserted through advance.insert_lot as world initialisation does."""
    from api import advance
    base = db.one(conn, "SELECT * FROM lots WHERE run_id = :r AND world = 'shelfshift' AND tag = 'designed'", r=run_id)
    row = {k: v for k, v in base.items() if k not in ('run_id', 'world')}
    row.update(lot_id=lot_id, qty=record, expiry_day=expiry, last_verified_day=verified, tag=None,
               branch=branch or base['branch'])
    for world in worlds or weekcheck.BENCH_WORLDS:
        advance.insert_lot(conn, run_id, world, row, physical)


def two_fact_shape(conn, run_id):
    """Two plan-changing STALE_COUNT facts on one case (checked on the engine's own output): the source lot (53 on the
    record, 50 physically) and a destination lot last counted long ago (4 on the record and physically)."""
    stale_designed_lot(conn, run_id)
    add_lot(conn, run_id, 'S11-P101-t9', 4, 4, expiry=1, branch='S11')


@pytest.fixture
def two_facts(eligible_dv, monkeypatch):
    run = make_run(eligible_dv, monkeypatch, two_fact_shape)
    yield run
    drop(run)


def history(conn, run, world=None, kind=None):
    return [h for h in db.rows(conn, 'SELECT * FROM evaluation_check_history WHERE sim_run_id = :r ORDER BY day, world, '
                                     'seq', r=run['run_id'])
            if (world is None or h['world'] == world) and (kind is None or h['event_kind'] == kind)]


def requests_of(conn, run, case_id=None):
    """The case's requests in the order the driver asked them (its durable request history: created_at is one now()
    per transaction, so it cannot order requests asked in the same morning)."""
    order = {h['request_id']: (h['day'], h['seq']) for h in history(conn, run, 'shelfshift', 'request')}
    rows = db.rows(conn, 'SELECT * FROM evidence_requests WHERE run_id = :r AND (CAST(:c AS text) IS NULL OR case_id = :c)',
                   r=run['run_id'], c=case_id)
    return sorted(rows, key=lambda q: order.get(q['request_id'], (999, 999)))


def test_a_case_with_two_count_facts_asks_both_on_its_version(two_facts):
    with db.tx() as conn:
        lot, case = designed_case(conn, two_facts)
        rec, facts = count_facts(conn, case)
        assert [f['lot_ids'] for f in facts] == [[lot['lot_id']], ['S11-P101-t9']], facts  # the engine's own order
        morning = weekcheck.checked_morning(conn, two_facts)
        asked = [q for q in requests_of(conn, two_facts, case['case_id']) if q['rec_version'] == rec['rec_version']]
        assert [q['fact_id'] for q in asked] == [f['fact_id'] for f in facts]
        assert {q['to_branch'] for q in asked} == {'S12', 'S11'}
        assert all(q['state'] == 'CONFIRMED' for q in asked)
        assert morning['asked'] >= 2 and morning['confirmed'] >= 2 and morning['refused'] == 0
        assert morning['lots_counted'] == 2  # requests and distinct lots are counted separately
        record = {l['lot_id']: l['qty'] for l in db.rows(conn, "SELECT lot_id, qty FROM lots WHERE run_id = :r AND "
                                                               "world = 'shelfshift'", r=two_facts['run_id'])}
        assert record[lot['lot_id']] == 50 and record['S11-P101-t9'] == 4


def test_the_cap_stops_a_version_and_its_unasked_fact_is_never_asked(two_facts, monkeypatch):
    from api import config
    monkeypatch.setattr(config, 'MAX_REQUESTS_PER_VERSION', 1)  # the settings value, lowered for this case only
    with db.tx() as conn:
        lot, case = designed_case(conn, two_facts)
        rec, facts = count_facts(conn, case)
        weekcheck.checked_morning(conn, two_facts)
        old = [q for q in requests_of(conn, two_facts, case['case_id']) if q['rec_version'] == rec['rec_version']]
        assert [q['fact_id'] for q in old] == [facts[0]['fact_id']]  # the second fact of that version: never asked
        capped = [h for h in history(conn, two_facts, 'shelfshift', 'refusal')
                  if h['rec_version'] == rec['rec_version'] and h['outcome']['code'] == 'too_many_requests']
        assert len(capped) == 1
        replanned = min(h['seq'] for h in history(conn, two_facts, 'shelfshift', 'replan')
                        if h['case_id'] == case['case_id'])
        later = [h for h in history(conn, two_facts, 'shelfshift') if h['case_id'] == case['case_id']
                 and h['seq'] > replanned and h['event_kind'] in ('request', 'observation')]
        assert later and all(h['rec_version'] != rec['rec_version'] for h in later)  # never the old version again


def test_a_count_that_replans_another_case_gives_it_its_own_checks(eligible_dv, monkeypatch):
    def shape(conn, run_id):
        stale_designed_lot(conn, run_id)
        add_lot(conn, run_id, 'S12-P101-t1', 20, 16, expiry=1)
    run = make_run(eligible_dv, monkeypatch, shape)
    try:
        with db.tx() as conn:
            lot, first = designed_case(conn, run)
            [other] = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND lot_id = 'S12-P101-t1'", r=run['run_id'])
            before = other['active_rec_version']
            weekcheck.checked_morning(conn, run)
            replans = [h for h in history(conn, run, 'shelfshift', 'replan') if h['case_id'] == other['case_id']]
            assert replans and replans[0]['outcome']['by_case'] == first['case_id']
            now = cases.case_row(conn, run['run_id'], other['case_id'])
            assert now['active_rec_version'] != before
            assert all(q['rec_version'] != before for q in requests_of(conn, run, other['case_id']))
    finally:
        drop(run)


def test_a_second_morning_the_same_day_asks_nothing_after_a_restart(two_facts):
    with db.tx() as conn:
        first = weekcheck.checked_morning(conn, two_facts)
    with db.tx() as conn:  # a re-entered driver: a new transaction rereads the durable history
        rows = len(history(conn, two_facts))
        again = weekcheck.checked_morning(conn, two_facts)
        assert first['asked'] > 0
        assert again == {'passes': 1, 'asked': 0, 'confirmed': 0, 'refused': 0, 'lots_counted': 0}
        assert len(history(conn, two_facts)) == rows


def test_every_asked_fact_writes_ordered_observations_for_both_worlds(two_facts):
    with db.tx() as conn:
        lot, case = designed_case(conn, two_facts)
        rec, facts = count_facts(conn, case)
        weekcheck.checked_morning(conn, two_facts, bench_id=two_facts['run_id'])
        rows = db.rows(conn, 'SELECT * FROM benchmark_observations WHERE benchmark_id = :b ORDER BY day, seq',
                       b=two_facts['run_id'])
        assert [r['seq'] for r in rows] == list(range(len(rows))) and len(rows) >= 2
        first = {r['lot_id']: r for r in rows if r['rec_version'] == rec['rec_version']}
        assert set(first) == {lot['lot_id'], 'S11-P101-t9'}
        mine = first[lot['lot_id']]
        assert mine['case_id'] == case['case_id'] and mine['request_id'] and mine['branch'] == 'S12'
        assert mine['results']['shelfshift'] == {'record_before': 53.0, 'physical': 50.0, 'correction': 'down'}
        assert mine['results']['baseline_checked'] == {'record_before': 53.0, 'physical': 50.0, 'correction': 'down'}
        assert mine['results']['confirm']['confirmed'] is True
        for world in ('shelfshift', 'baseline_checked'):
            seen = [h['lot_id'] for h in history(conn, two_facts, world, 'observation')]
            assert sorted(seen) == sorted(set(seen))  # each lot observed once a day per world
            assert all(h['benchmark_id'] == two_facts['run_id'] for h in history(conn, two_facts, world))


def test_a_confirm_refusal_is_recorded_and_never_retried(two_facts, monkeypatch):
    real = signals._inapplicable

    def moved_away(conn, run, q, lot_ids):  # the destination lot left the record between answer and confirm
        return ('S11-P101-t9', 'Lot S11-P101-t9 is no longer on the record at S11') if 'S11-P101-t9' in lot_ids \
            else real(conn, run, q, lot_ids)
    monkeypatch.setattr(signals, '_inapplicable', moved_away)
    with db.tx() as conn:
        first = weekcheck.checked_morning(conn, two_facts)
        refused = [h for h in history(conn, two_facts, 'shelfshift', 'refusal') if h['outcome']['stage'] == 'confirm']
        assert [h['outcome']['code'] for h in refused] == ['count_mismatch'] and first['refused'] >= 1
        [q] = [q for q in requests_of(conn, two_facts) if q['request_id'] == refused[0]['request_id']]
        assert q['state'] == 'ANSWERED'  # the answer stays; nothing is confirmed with altered values
        assert weekcheck.checked_morning(conn, two_facts)['asked'] == 0
        assert len([h for h in history(conn, two_facts, 'shelfshift', 'refusal')
                    if h['outcome']['stage'] == 'confirm']) == 1
        assert db.one(conn, "SELECT qty FROM lots WHERE run_id = :r AND world = 'baseline_checked' "
                            "AND lot_id = 'S11-P101-t9'", r=two_facts['run_id'])['qty'] == 4  # no correction either


def test_overlapping_groups_keep_their_exact_lot_sets_and_observe_each_lot_once(eligible_dv, monkeypatch):
    """Groups {A,B} then {B,C} (then {A,C}, all observed) on one version. The engine cannot give partly overlapping
    groups within one day on this tiny set (a branch's phantom group only shrinks as its lots are counted), so this one
    test presents the plan in force's checks through a test seam on selection.plan_in_force, which both the asking
    loop (commitment.checks_in_force) and the request service (signals._flagged, since Phase 7) read; requests,
    answers, confirmations and both worlds' records are the real service path."""
    from api import config, selection

    def shape(conn, run_id):
        stale_designed_lot(conn, run_id)
        add_lot(conn, run_id, 'S12-P101-tb', 9, 7, expiry=2)
        add_lot(conn, run_id, 'S12-P101-tc', 8, 8, expiry=2)
    run = make_run(eligible_dv, monkeypatch, shape)
    real = selection.plan_in_force
    try:
        with db.tx() as conn:
            lot, case = designed_case(conn, run)
            rec = cases.active_rec(conn, case)
            a = lot['lot_id']
            template = next(f for f in rec['fact_checks'] if f['lot_ids'] == [a])

            def group(fid, lots):
                return {**template, 'fact_id': fid, 'kind': 'PHANTOM_STOCK', 'lot_id': lots[0], 'lot_ids': lots,
                        'branch': 'S12', 'changes_plan': True}
            groups = [group('g-ab', [a, 'S12-P101-tb']), group('g-bc', ['S12-P101-tb', 'S12-P101-tc']),
                      group('g-ac', [a, 'S12-P101-tc'])]

            def in_force(c, r):
                key, checks, sel = real(c, r)
                if r and r['rec_version'] == rec['rec_version']:
                    return key, groups, sel
                return key, [f for f in checks if not f['changes_plan']], sel
            monkeypatch.setattr(selection, 'plan_in_force', in_force)
            monkeypatch.setattr(config, 'MAX_REQUESTS_PER_VERSION', 3)
            morning = weekcheck.checked_morning(conn, run)
            asked = requests_of(conn, run, case['case_id'])
            assert [(q['fact_id'], q['lot_ids']) for q in asked] == [
                ('g-ab', [a, 'S12-P101-tb']), ('g-bc', ['S12-P101-tb', 'S12-P101-tc'])]  # g-ac: all observed
            assert morning['asked'] == 2 and morning['lots_counted'] == 3
            second = db.one(conn, 'SELECT fields FROM signals WHERE run_id = :r AND request_id = :q',
                            r=run['run_id'], q=asked[1]['request_id'])['fields']
            assert second['counts'] == {'S12-P101-tb': {'shelf_units': 7.0, 'backroom_units': 0.0},
                                        'S12-P101-tc': {'shelf_units': 8.0, 'backroom_units': 0.0}}
            for world in ('shelfshift', 'baseline_checked'):
                seen = sorted(h['lot_id'] for h in history(conn, run, world, 'observation'))
                assert seen == sorted([a, 'S12-P101-tb', 'S12-P101-tc'])
            assert all(q['state'] == 'CONFIRMED' for q in asked)
    finally:
        drop(run)


# ---- Task 3: D-10 and D-11 acceptance ------------------------------------------------------------------------------

def lot_qty(conn, run, world, lot_id):
    row = db.one(conn, 'SELECT qty FROM lots WHERE run_id = :r AND world = :w AND lot_id = :l',
                 r=run['run_id'], w=world, l=lot_id)
    return None if row is None else row['qty']


def compare_observation(conn, run, lot_id):
    [h] = [h for h in history(conn, run, 'baseline_checked', 'observation') if h['lot_id'] == lot_id]
    return h['outcome']


def test_d10_the_comparison_count_confirms_its_own_twenty_never_shelfshifts_thirty(eligible_dv, monkeypatch):
    """D-10's example: ShelfShift's world holds 30 units of the lot; the store rule already shipped 10 of them in its
    own world, so 20 are left there (set test-locally on that world's physical stock only)."""
    def shape(conn, run_id):
        stale_designed_lot(conn, run_id)
        db.run(conn, "UPDATE truth_lots SET qty = 30 WHERE run_id = :r AND lot_id = 'S12-P101--1-r'", r=run_id)
        db.run(conn, "UPDATE truth_lots SET qty = 20 WHERE run_id = :r AND world = 'baseline_checked' "
                     "AND lot_id = 'S12-P101--1-r'", r=run_id)
    run = make_run(eligible_dv, monkeypatch, shape)
    try:
        with db.tx() as conn:
            morning = weekcheck.checked_morning(conn, run, bench_id=run['run_id'])
            assert morning['confirmed'] >= 1
            assert lot_qty(conn, run, 'shelfshift', 'S12-P101--1-r') == 30
            assert lot_qty(conn, run, 'baseline_checked', 'S12-P101--1-r') == 20
            assert compare_observation(conn, run, 'S12-P101--1-r') == {
                'record_before': 53.0, 'physical': 20.0, 'correction': 'down', 'recorded_units': 53.0,
                'observed_units': 20.0}
            row = db.one(conn, "SELECT results FROM benchmark_observations WHERE benchmark_id = :b "
                               "AND lot_id = 'S12-P101--1-r'", b=run['run_id'])['results']
            assert row['shelfshift']['physical'] == 30.0 and row['baseline_checked']['physical'] == 20.0
            assert lot_qty(conn, run, 'baseline_rule', 'S12-P101--1-r') == 53  # scenario 1: original records
    finally:
        drop(run)


def test_a_lot_already_gone_in_the_comparison_world_is_checked_with_nothing_to_correct(eligible_dv, monkeypatch):
    def shape(conn, run_id):  # sold out or expired in the comparison world only
        stale_designed_lot(conn, run_id)
        for table in ('truth_lots', 'lots'):
            db.run(conn, f"DELETE FROM {table} WHERE run_id = :r AND world = 'baseline_checked' "
                         f"AND lot_id = 'S12-P101--1-r'", r=run_id)
    run = make_run(eligible_dv, monkeypatch, shape)
    try:
        with db.tx() as conn:
            assert weekcheck.checked_morning(conn, run)['confirmed'] >= 1
            assert lot_qty(conn, run, 'shelfshift', 'S12-P101--1-r') == 50
            assert compare_observation(conn, run, 'S12-P101--1-r') == {
                'record_before': None, 'physical': None, 'correction': 'gone', 'recorded_units': 0.0,
                'observed_units': 0.0}
            assert lot_qty(conn, run, 'baseline_checked', 'S12-P101--1-r') is None  # nothing appears there
    finally:
        drop(run)


def test_a_moved_lot_that_exists_only_in_shelfshift_is_gone_in_the_comparison_world(eligible_dv, monkeypatch):
    def shape(conn, run_id):  # a moved lot id: only ShelfShift's own transfer created it
        stale_designed_lot(conn, run_id)
        add_lot(conn, run_id, 'S11-P101-t9', 4, 4, expiry=1, branch='S11', worlds=('shelfshift',))
        db.run(conn, "UPDATE lots SET moved_from = 'S12-P101--1-r' WHERE run_id = :r AND lot_id = 'S11-P101-t9'",
               r=run_id)
    run = make_run(eligible_dv, monkeypatch, shape)
    try:
        with db.tx() as conn:
            weekcheck.checked_morning(conn, run)
            assert [h['lot_id'] for h in history(conn, run, 'shelfshift', 'observation')
                    if h['lot_id'] == 'S11-P101-t9'] == ['S11-P101-t9']
            assert compare_observation(conn, run, 'S11-P101-t9')['correction'] == 'gone'
            assert lot_qty(conn, run, 'baseline_checked', 'S11-P101-t9') is None
    finally:
        drop(run)


def test_without_record_errors_scenarios_one_and_two_stay_identical_with_real_checks(eligible_dv, monkeypatch):
    """No record errors anywhere (the designed lot's record equals its physical stock; the other stores' stock of its
    product is removed), yet the lot was last counted long ago, so the engine flags a plan-changing count that is
    really requested, answered and confirmed in both checked worlds. Over three days the store rule with original
    records and the store rule with equivalent stock checks act and end identically."""
    from api import advance, evalref, refdata

    def shape(conn, run_id):
        stale_designed_lot(conn, run_id)
        db.run(conn, "UPDATE lots SET qty = 50 WHERE run_id = :r AND lot_id = 'S12-P101--1-r'", r=run_id)
    run = make_run(eligible_dv, monkeypatch, shape)
    strip = lambda rows: sorted(({k: v for k, v in r.items() if k not in ('run_id', 'world', 'last_verified_day',
                                                                          'verified_by')} for r in rows), key=repr)
    try:
        with db.tx() as conn:
            errors = db.rows(conn, 'SELECT l.lot_id FROM lots l JOIN truth_lots t USING (run_id, world, lot_id) '
                                   'WHERE l.run_id = :r AND l.qty <> t.qty', r=run['run_id'])
            assert errors == []
        with evalref.forecast_rule(run['dataset_version']):
            for day in range(3):
                with db.tx() as conn:
                    current = db.one(conn, 'UPDATE demo_runs SET day = :d WHERE run_id = :r RETURNING *',
                                     r=run['run_id'], d=day)
                    if day:
                        advance.day_start(conn, current, worlds=weekcheck.BENCH_WORLDS)
                        advance.cases_morning(conn, current, eager=True)
                    weekcheck.checked_morning(conn, current, bench_id=run['run_id'])
                    for world in ('shelfshift', 'baseline_checked'):  # each own record equals its own physical
                        for h in history(conn, current, world, 'observation'):
                            if h['day'] == day and h['outcome']['correction'] != 'gone':
                                assert lot_qty(conn, current, world, h['lot_id']) == h['outcome']['observed_units']
                    weekcheck.world_steps(conn, current, refdata.truth(current['dataset_version']))
                    for table in ('world_actions', 'outcomes', 'world_events', 'lots'):
                        rows = {w: db.rows(conn, f'SELECT * FROM {table} WHERE run_id = :r AND world = :w',
                                           r=run['run_id'], w=w) for w in ('baseline_rule', 'baseline_checked')}
                        assert strip(rows['baseline_rule']) == strip(rows['baseline_checked']), (day, table)
        with db.tx() as conn:
            for world in ('shelfshift', 'baseline_checked'):
                assert len(history(conn, run, world, 'request')) > 0
                assert len(history(conn, run, world, 'confirm')) > 0
                assert all(h['outcome']['correction'] in ('none', 'gone')
                           for h in history(conn, run, world, 'observation'))
    finally:
        drop(run)


def test_after_a_replan_no_new_check_names_the_old_version(two_facts):
    with db.tx() as conn:
        lot, case = designed_case(conn, two_facts)
        rec, facts = count_facts(conn, case)
        weekcheck.checked_morning(conn, two_facts, bench_id=two_facts['run_id'])
        rows = history(conn, two_facts, 'shelfshift')
        replanned = min(h['seq'] for h in rows if h['event_kind'] == 'replan' and h['case_id'] == case['case_id'])
        asked_before = {h['request_id'] for h in rows if h['event_kind'] == 'request' and h['seq'] < replanned}
        later = [h for h in rows if h['seq'] > replanned and h['case_id'] == case['case_id']
                 and h['rec_version'] == rec['rec_version']]
        assert all(h['event_kind'] != 'request' for h in later)  # no old-version check is asked after the replan
        assert all(h['request_id'] in asked_before for h in later)  # only the answers to checks asked before it
        observed = db.rows(conn, 'SELECT * FROM benchmark_observations WHERE benchmark_id = :b ORDER BY seq',
                           b=two_facts['run_id'])
        assert all(o['request_id'] in asked_before for o in observed if o['rec_version'] == rec['rec_version'])
