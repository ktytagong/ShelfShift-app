"""The template build's batched reads and writes give the rows the per-product code gave (quick 261018-4nj).

BASE is fb4ddc01d0ae6e2ac891542b1a925beb8907fed6 (the 05.2 WEB_REF), with 06.1 D-34's dispatched-transfer condition on
the inbound delete since the release merge (build/web dc0448e). Each oracle below is BASE's code copied verbatim
(only its name changed), so a batched site is held to the exact rows, results and messages the per-product code
produced, at PRODUCT_BATCH 1, 2 and its default. A statement-bound test per site fails on BASE, where the count grows
with the number of products, and passes after. Every step runs in a transaction that is rolled back: the scratch runs
are never changed by a comparison."""
from __future__ import annotations

import sys
import uuid

import pytest
import sqlalchemy as sa

from api import advance, db, inputs, overlay, refdata, sandbox
from api.advance import (MOVED, MOVED_SEQ, OUTCOME_COLUMNS, OUTCOME_FIELDS, TODAY_ACTIONS, _void_repeated_price_changes,
                         insert_lots)
from engine.allocator import ShelfLot
from engine.timing import first_sellable_day
from engine.world import PriceChange, Shipment, step_day
from tests.api.conftest import drop_runs

HERE = sys.modules[__name__]
BATCHES = (1, 2, None)  # None: the default PRODUCT_BATCH
COMPARED = ("lots", "truth_lots", "inbound", "outcomes", "world_events", "world_actions", "reservations",
            "activity_log")
DROP = {"reservations": ("res_id",), "activity_log": ("seq",)}  # a random uuid; a global sequence
ORDERED = {"activity_log": "seq"}  # compared in this order (the others as sorted rows)


# ---------------------------------------------------------------------------------------------- BASE's step_world
# api/advance.py lines 203-305 at BASE, verbatim but for the name; the helpers it calls (_void_repeated_price_changes,
# insert_lots) are unchanged by quick 261018-4nj and imported from api/advance.py.

def oracle_step_world(conn, run: dict, world: str, truth: dict) -> None:
    """Step one world one day, product by product, through engine.world.step_day; store its output as returned.

    Hidden demand covers only the stores that carry the product (a public set's real series, 05.2 D-11). step_day
    still gets a demand for every store, 0.0 for a store without the product (it sells nothing there), and an
    outcome row is written only for the stores that carry the product that day: hidden demand, the product's lots,
    today's arrivals and every shipment's destination and origin, one multi-row INSERT per product (quick
    261017-p52). A nonzero outcome anywhere else raises; it is never dropped. On the synthetic set every store
    carries every product, so the rows are as before."""
    ref = refdata.get(run["dataset_version"])
    run_id, day = run["run_id"], run["day"]
    policy = inputs.sandbox_policy(conn, run, ref)  # one Policy per sandbox in every world (D-09; research A17)
    m = {20: truth.price_response["m20"], 30: truth.price_response["m30"]}
    seq = 0
    for sku in sorted(ref.products):
        product = ref.products[sku]
        rows = {l["lot_id"]: l for l in db.rows(conn, "SELECT * FROM lots WHERE run_id = :r AND world = :w AND sku = :s "
                                                      "ORDER BY lot_id", r=run_id, w=world, s=sku)}
        phys = {t["lot_id"]: t["qty"] for t in db.rows(conn, "SELECT lot_id, qty FROM truth_lots WHERE run_id = :r "
                                                             "AND world = :w AND lot_id = ANY(:ids)",
                                                       r=run_id, w=world, ids=list(rows))}
        actions = db.rows(conn, f"SELECT * FROM world_actions WHERE run_id = :r AND world = :w AND sku = :s "
                                f"AND state = 'PLANNED' AND {TODAY_ACTIONS} ORDER BY action_id",
                          r=run_id, w=world, s=sku, d=day)
        later = {a["lot_id"]: a["qty"] for a in db.rows(conn, "SELECT lot_id, sum(qty) AS qty FROM world_actions "
                                                              "WHERE run_id = :r AND world = :w AND sku = :s "
                                                              "AND state = 'PLANNED' AND kind = 'TRANSFER' "
                                                              "AND run_day > :d GROUP BY lot_id",
                                                        r=run_id, w=world, s=sku, d=day)}
        lots = [ShelfLot(l["lot_id"], l["branch"], phys.get(l["lot_id"], l["qty"]), l["expiry_day"], l["received_seq"],
                         l["markdown_pct"], recorded=l["qty"], reserved=later.get(l["lot_id"], 0.0))
                for l in rows.values()]
        # deliveries the day start did not move (none of today's 06:00 ones): sellable per the sandbox Policy
        arriving = db.rows(conn, "SELECT * FROM inbound WHERE run_id = :r AND world = :w AND sku = :s "
                                 "AND arrival_day = :d AND source_case_id IS NULL ORDER BY inbound_id",
                           r=run_id, w=world, s=sku, d=day)
        physical = truth.physical_of([i["inbound_id"] for i in arriving])
        arrivals = tuple(ShelfLot(i["inbound_id"], i["branch"], physical.get(i["inbound_id"], i["qty"]),
                                  i["expiry_day"], i["received_seq"],
                                  sellable_from=first_sellable_day(day, i["arrival_min"], policy),
                                  recorded=i["qty"]) for i in arriving)
        meta = {**rows, **{i["inbound_id"]: {**i, "lot_id": i["inbound_id"], "markdown_pct": 0, "verified_by": "COUNT",
                                             "moved_from": None} for i in arriving}}
        shipments = []
        for a in (a for a in actions if a["kind"] == "TRANSFER"):
            shipments.append(Shipment(lot_id=a["lot_id"], qty=a["qty"], dest=a["dest"], new_lot_id=a["new_lot_id"],
                                      sellable_from=a["sellable_from"], fixed_cost=a["fixed_cost"],
                                      per_unit_cost=a["per_unit_cost"], cancel_cost=a["cancel_cost"],
                                      origin=a["origin"]))
            src = meta.get(a["lot_id"])  # a gone source lot is a failed delivery inside step_day: nothing arrives
            if src is not None:
                meta[a["new_lot_id"]] = {**src, "lot_id": a["new_lot_id"], "branch": a["dest"], "received_seq": MOVED_SEQ,
                                         "markdown_pct": 0, "last_verified_day": day, "tag": None, "origin": MOVED,
                                         "moved_from": a["lot_id"]}
        markdowns = [a for a in actions if a["kind"] == "MARKDOWN"]
        markdowns = _void_repeated_price_changes(conn, run, world, markdowns, {l.lot_id: l.pct for l in lots})
        prices = tuple(PriceChange(a["lot_id"], a["pct"], day) for a in markdowns)
        hidden = truth.demand_for(sku, day)  # one product's day: the stores that carry it (05.2 D-11)
        demand = {b: hidden.get(b, 0.0) for b in ref.branches}  # a store without the product sells nothing
        # the stores that carry the product today, taken before step_day mutates lots
        carrying = (set(hidden) | {l.branch for l in lots} | {a.branch for a in arrivals}
                    | {sh.dest for sh in shipments} | {sh.origin for sh in shipments})
        tracked = [l for l in lots if ">" in l.lot_id]
        out = step_day(lots, day, demand, m, product, tuple(shipments), prices, arrivals, schedule=ref.schedules[sku])
        for name in OUTCOME_FIELDS:  # an outcome outside the carrying set is a bug, never dropped silently
            for b, value in getattr(out, name).items():
                if value and b not in carrying:
                    raise RuntimeError(f"{world} day {day} {sku}: {name} {value!r} at {b}, a store without the "
                                       f"product that day")
        db.insert_rows(conn, "outcomes", OUTCOME_COLUMNS, [
            (run_id, world, day, b, sku, demand[b], out.sold.get(b, 0.0), out.wasted.get(b, 0.0), out.lost.get(b, 0.0),
             out.revenue.get(b, 0.0), out.transport.get(b, 0.0), out.handling.get(b, 0.0), out.disposal.get(b, 0.0))
            for b in ref.branches if b in carrying])
        events = [("SHORT_SHIPMENT", s) for s in out.short_shipments]
        events += [("RECORD_CORRECTION", c) for c in out.record_corrections]
        shipped = {s["lot_id"]: s["shipped"] for s in out.short_shipments}
        events += [("TRANSFER_RECEIVED", {"lot_id": sh.new_lot_id, "branch": sh.dest, "qty": shipped.get(sh.lot_id, sh.qty)})
                   for sh in shipments if shipped.get(sh.lot_id, sh.qty) > 0]
        events += [("TRANSFER_EXPIRED", {"lot_id": l.lot_id, "branch": l.branch, "wasted": l.qty})
                   for l in tracked if l not in lots]
        for kind, payload in events:
            db.run(conn, "INSERT INTO world_events (run_id, world, day, seq, kind, payload) "
                         "VALUES (:r, :w, :d, :n, :k, CAST(:p AS jsonb))",
                   r=run_id, w=world, d=day, n=seq, k=kind, p=db.js({**payload, "sku": sku}))
            seq += 1
        db.run(conn, "DELETE FROM truth_lots WHERE run_id = :r AND world = :w AND lot_id = ANY(:ids)",
               r=run_id, w=world, ids=list(rows))
        db.run(conn, "DELETE FROM lots WHERE run_id = :r AND world = :w AND sku = :s", r=run_id, w=world, s=sku)
        insert_lots(conn, run_id, world, [
            ({**meta[l.lot_id], "lot_id": l.lot_id, "branch": l.branch, "sku": sku, "qty": round(l.recorded, 6),
              "expiry_day": l.expiry_day, "received_seq": l.received_seq, "markdown_pct": l.pct}, round(l.qty, 6))
            for l in lots])
        # a shipment became its moved lot today, whatever its arrival day: its pending delivery goes with it, so
        # the stock is never counted twice at the destination (WR-09). A dispatched transfer's delivery stays until
        # its receipt removes it (06.1 D-34): it is still on its way, and nothing has received it
        db.run(conn, "DELETE FROM inbound WHERE run_id = :r AND world = :w AND sku = :s "
                     "AND (arrival_day <= :d OR inbound_id = ANY(:m)) AND inbound_id NOT IN (SELECT a.new_lot_id "
                     "FROM world_actions a WHERE a.run_id = :r AND a.world = :w AND a.state = 'DISPATCHED' "
                     "AND a.new_lot_id IS NOT NULL)", r=run_id, w=world, s=sku, d=day,
               m=[sh.new_lot_id for sh in shipments])
        db.run(conn, f"UPDATE world_actions SET state = 'DONE' WHERE run_id = :r AND world = :w AND sku = :s "
                     f"AND state = 'PLANNED' AND {TODAY_ACTIONS}", r=run_id, w=world, s=sku, d=day)
        for a in (a for a in actions if a["case_id"]):  # the set-aside left with the truck; the price is on the lot now
            kinds = ["SOURCE_LOT", "TRUCK"] if a["kind"] == "TRANSFER" else ["LOT_PRICE"]
            db.run(conn, "UPDATE reservations SET state = 'DONE' WHERE run_id = :r AND case_id = :c AND kind = ANY(:k) "
                         "AND state = 'COMMITTED'", r=run_id, c=a["case_id"], k=kinds)


# ---------------------------------------------------------------------------------------------- scratch runs

class _Rollback(Exception):
    """Raised inside a db.tx() to roll it back."""


def _snapshot(conn, run_id: str) -> dict[str, list[str]]:
    """Every compared table's rows of the run as jsonb text, without run_id, the dropped columns and every
    timestamp column; sorted, except activity_log in seq order."""
    out = {}
    for t in COMPARED:
        ts = [r["column_name"] for r in db.rows(conn, "SELECT column_name FROM information_schema.columns WHERE "
                                                      "table_schema = current_schema() AND table_name = :t AND "
                                                      "data_type LIKE 'timestamp%' ORDER BY 1", t=t)]
        minus = "".join(f" - '{c}'" for c in ("run_id", *DROP.get(t, ()), *ts))
        order = f"x.{ORDERED[t]}" if t in ORDERED else "j"
        out[t] = [r["j"] for r in db.rows(conn, f"SELECT (to_jsonb(x){minus})::text AS j FROM {t} x "
                                                f"WHERE x.run_id = :r ORDER BY {order}", r=run_id)]
    return out


def _stepped(run_id: str, world: str, step) -> dict[str, list[str]]:
    """The run's rows after `step` stepped one world, in a transaction rolled back afterwards."""
    got = {}
    with pytest.raises(_Rollback):
        with db.tx() as conn:
            run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)
            step(conn, run, world, refdata.truth(run["dataset_version"]))
            got.update(_snapshot(conn, run_id))
            raise _Rollback
    return got


def _today(run_id: str, world: str) -> list[dict]:
    with db.tx() as conn:
        run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)
        return db.rows(conn, f"SELECT * FROM world_actions WHERE run_id = :r AND world = :w AND state = 'PLANNED' "
                             f"AND {TODAY_ACTIONS}", r=run_id, w=world, d=run["day"])


@pytest.fixture(scope="module")
def synthetic_run(template):
    """A workspace cloned from the synthetic ACTIVE template, owning every series (as advance_day makes it first),
    the autopilot's work orders prepared and the store rule's morning run, committed: its run_id."""
    with db.tx() as conn:
        rid = sandbox.clone(conn, sandbox.active_template(conn))
    try:
        with db.tx() as conn:
            run = db.lock_run(conn, rid, mode="UPDATE")
            overlay.materialize_all(conn, run)
            sandbox.autopilot(conn, run)
        with db.tx() as conn:
            advance.baseline_morning(conn, db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=rid))
        yield rid
    finally:
        with db.tx() as conn:
            drop_runs(conn, [rid])


@pytest.fixture(scope="module")
def tiny_run(public_dv):
    """A day-0 TEMPLATE of the tiny public set built as conftest's public_run is, then the store rule's morning,
    committed: its run_id. INITIALIZING, never ACTIVE."""
    rid = f"tpl-pub-{uuid.uuid4().hex[:8]}"
    try:
        with db.tx() as conn:
            db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) VALUES "
                         "(:r, 'TEMPLATE', :v, 'INITIALIZING', 0, 1)", r=rid, v=public_dv)
            sandbox.init_worlds(conn, rid, refdata.get(public_dv), refdata.truth(public_dv))
            advance.day_start(conn, db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=rid))
        with db.tx() as conn:
            advance.baseline_morning(conn, db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=rid))
        yield rid
    finally:
        with db.tx() as conn:
            drop_runs(conn, [rid])


def _batch(monkeypatch, size):
    if size is not None:
        monkeypatch.setattr(inputs, "PRODUCT_BATCH", size)


# ---------------------------------------------------------------------------------------------- step_world

@pytest.mark.parametrize("size", BATCHES)
@pytest.mark.parametrize("which", ["synthetic_run", "tiny_run"])
def test_step_world_writes_the_rows_the_per_product_step_wrote(request, monkeypatch, which, size):
    rid = request.getfixturevalue(which)
    _batch(monkeypatch, size)
    with db.tx() as conn:
        before = len(_snapshot(conn, rid)["outcomes"])
    for world in advance.WORLDS:
        want = _stepped(rid, world, oracle_step_world)
        got = _stepped(rid, world, advance.step_world)
        for table in COMPARED:
            assert got[table] == want[table], (which, size, world, table)
        assert len(want["outcomes"]) > before, (which, world)  # not vacuous: the day was written
    if which == "synthetic_run":  # and today's work orders were executed, the cases' bookings with them
        assert _today(rid, "baseline_rule")
        assert any(a["case_id"] for a in _today(rid, "shelfshift"))


def _carrying_guard(conn, run, world, truth, step):
    try:
        step(conn, run, world, truth)
    except RuntimeError as exc:
        return str(exc)
    return None


def test_an_outcome_outside_the_carrying_set_still_raises_the_same_error(synthetic_run, monkeypatch):
    real = step_day

    def leaky(*args, **kwargs):  # test-local: one sale at a store that does not exist
        out = real(*args, **kwargs)
        out.sold["NO-SUCH-STORE"] = 1.0
        return out

    monkeypatch.setattr(advance, "step_day", leaky)
    monkeypatch.setattr(HERE, "step_day", leaky)
    messages = []
    for step in (oracle_step_world, advance.step_world):
        with pytest.raises(_Rollback):
            with db.tx() as conn:
                run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=synthetic_run)
                messages.append(_carrying_guard(conn, run, "shelfshift", refdata.truth(run["dataset_version"]), step))
                raise _Rollback
    assert messages[0] and messages[0] == messages[1]
    assert "NO-SUCH-STORE, a store without the product that day" in messages[0]


CHUNKED = ("outcomes", "world_events", "lots", "truth_lots")  # written through db.insert_rows (one per chunk)
# per product batch, at most: 5 reads (lots, truth_lots, today's actions, later transfers, arriving inbound), one
# truth.physical_of of the arrivals, one hidden-demand read, the truth_lots, lots and inbound DELETEs, the world_actions
# UPDATE and two reservations UPDATEs: 5 + 1 + 1 + 3 + 1 + 2 = 13
K_STEP_WORLD = 13


def _statements(fn) -> list[str]:
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()))

    sa.event.listen(db.engine(), "before_cursor_execute", record)
    try:
        fn()
    finally:
        sa.event.remove(db.engine(), "before_cursor_execute", record)
    return seen


def test_step_world_sends_a_constant_number_of_statements_per_product_batch(synthetic_run):
    """One synthetic world: at most K_STEP_WORLD statements per product batch, plus one per insert_rows chunk and two
    per voided price change (its UPDATE and its log line). Per product (BASE) it was at least 9 per product."""
    seen: list[str] = []
    with pytest.raises(_Rollback):
        with db.tx() as conn:
            run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=synthetic_run)
            ref, truth = refdata.get(run["dataset_version"]), refdata.truth(run["dataset_version"])
            seen = _statements(lambda: advance.step_world(conn, run, "shelfshift", truth))
            raise _Rollback
    batches = -(-len(ref.products) // inputs.PRODUCT_BATCH)
    chunks = [s for s in seen if any(s.startswith(f"INSERT INTO {t} (") for t in CHUNKED)]
    voided = [s for s in seen if s.startswith("UPDATE world_actions SET state = 'VOIDED'")]
    logs = [s for s in seen if s.startswith("INSERT INTO activity_log")]
    rest = len(seen) - len(chunks) - len(voided) - len(logs)
    print(f"\nstep_world shelfshift: {len(ref.products)} products, {batches} batches, {len(seen)} statements, "
          f"{len(chunks)} chunks, {len(voided)} voided, {rest} others")
    assert len(logs) == len(voided)
    assert rest <= K_STEP_WORLD * batches, (rest, batches, seen)
    # insert_rows sends n // 512 chunks plus one per set bit of n % 512; every table gets fewer than 512 rows per
    # batch here, so at most 9 chunks per table and batch
    assert len(chunks) <= batches * len(CHUNKED) * 9, (len(chunks), batches)
