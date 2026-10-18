"""Public artifact sets in shared relational tables, read one series at a time (spec §4 Data flow 1, §5 Public dataset;
DATA-08, DATA-15; 05.2-CONTEXT D-08, D-11, D-15).

fixtures/public_tiny is a byte-for-byte copy of build/web's tests/data/fixtures/public_tiny (committed there in 4c08c99,
ds-85d230e81d20): a synthetic test fixture in the FreshRetailNet-50K schema, never public rows. Every expected value is
read from the set's own parts here (gzip + csv), never pasted from output. A refusal test installs a variant of the set
with its own dataset_version (the manifest's inputs renamed, the version recomputed), so it never meets the version the
session already installed, and it then checks that no row of that version exists in any ref_* table.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import pathlib
import re
import shutil
import uuid

import pytest
import sqlalchemy as sa

from api import db, publicref, refdata
from data import versions
from tests.api.conftest import TINY, restore_the_designed_flag

MANIFEST = "manifest.json"


# ------------------------------------------------------------------------------------------------ the set's own parts

def manifest(root: pathlib.Path = TINY) -> dict:
    return json.loads((root / MANIFEST).read_text(encoding="utf-8"))


def part_rows(part: str, root: pathlib.Path = TINY) -> list[dict]:
    """A part's rows as dicts of strings, read from its gz CSV."""
    with gzip.open(root / manifest(root)["files"][part]["path"], "rt", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_manifest(root: pathlib.Path, m: dict) -> dict:
    """The manifest written back with its dataset_version recomputed (data.versions.digest, as the build does)."""
    m["dataset_version"] = versions.digest("ds-", {k: v for k, v in m.items() if k != "dataset_version"})
    (root / MANIFEST).write_text(json.dumps(m, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return m


def variant(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    """A copy of the tiny set with a dataset_version of its own (its inputs renamed), else unchanged."""
    root = tmp_path / name
    shutil.copytree(TINY, root)
    m = manifest(root)
    m["inputs"]["dataset"] = f"{m['inputs']['dataset']} (test variant {name})"
    write_manifest(root, m)
    return root


def rewrite_part(root: pathlib.Path, part: str, header: list[str], rows: list[list[str]]) -> None:
    """A part rewritten (header and rows), its sha256 and row count updated in the manifest, the version recomputed:
    only the checks that read the rows themselves can then refuse it."""
    m = manifest(root)
    entry = m["files"][part]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    data = gzip.compress(buf.getvalue().encode("utf-8"), mtime=0)
    (root / entry["path"]).write_bytes(data)
    entry.update(sha256=hashlib.sha256(data).hexdigest(), rows=len(rows))
    write_manifest(root, m)


def rows_of(dv: str) -> dict[str, int]:
    """Rows of `dv` in ref_set and every reference table."""
    with db.tx() as conn:
        out = {t: db.one(conn, f"SELECT count(*) AS n FROM {t} WHERE dataset_version = :v", v=dv)["n"]
               for t in publicref.ALLOWED_TABLES}
        out["ref_set"] = db.one(conn, "SELECT count(*) AS n FROM ref_set WHERE dataset_version = :v", v=dv)["n"]
    return out


def refused(root: pathlib.Path, match: str, allow_local: bool = True) -> None:
    """install refuses the set with a ValueError matching `match`, and nothing of its version is left behind."""
    dv = manifest(root)["dataset_version"]
    with pytest.raises(ValueError, match=match):
        refdata.install(root, allow_local=allow_local)
    assert not any(rows_of(dv).values()), rows_of(dv)


# ------------------------------------------------------------------------------------------------ install

def test_the_tiny_set_installs_in_full_into_shared_tables_once(public_dv):
    m = manifest()
    assert public_dv == m["dataset_version"]
    counts = rows_of(public_dv)
    assert counts.pop("ref_set") == 1
    assert counts == {e["table"]: e["rows"] for e in m["files"].values()}  # every row of every part, none subset away
    with db.tx() as conn:
        kind = db.one(conn, "SELECT kind FROM ref_set WHERE dataset_version = :v", v=public_dv)["kind"]
        with_run = db.rows(conn, "SELECT table_name FROM information_schema.columns WHERE table_schema = "
                                 "current_schema() AND column_name = 'run_id' AND table_name LIKE 'ref%'")
    assert kind == "public"
    assert with_run == []  # no clone or prune can touch a shared reference table (D-08)
    assert refdata.install(TINY, allow_local=True) == public_dv  # a second install is a no-op
    assert rows_of(public_dv) == {**counts, "ref_set": 1}


def test_allowed_tables_are_the_twelve_parts_tables():
    assert sorted(publicref.ALLOWED_TABLES) == sorted(e["table"] for e in manifest()["files"].values())


def test_one_changed_byte_in_a_part_is_refused_naming_the_part(schema, tmp_path):
    root = variant(tmp_path, "byte")
    path = root / manifest(root)["files"]["lots0"]["path"]
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))
    refused(root, r"parts/lots0\.csv\.gz: sha256")


def test_a_part_naming_a_table_outside_the_allowed_list_is_refused(schema, tmp_path):
    root = variant(tmp_path, "table")
    m = manifest(root)
    m["files"]["weather"]["table"] = "demo_runs"
    write_manifest(root, m)
    refused(root, r"parts/weather\.csv\.gz: table 'demo_runs' is not")


def test_a_header_in_another_column_order_than_the_table_is_refused(schema, tmp_path):
    root = variant(tmp_path, "order")
    rows = part_rows("capacity", root)
    header = ["zone", "store", "day", "free_totes"]  # the part's own columns, store and zone swapped
    rewrite_part(root, "capacity", header, [[r[c] for c in header] for r in rows])
    m = manifest(root)
    m["files"]["capacity"]["columns"] = header  # the manifest agrees with the part; the table does not
    write_manifest(root, m)
    refused(root, r"parts/capacity\.csv\.gz: columns .* are not ref_capacity's")


def test_a_header_other_than_the_manifest_columns_is_refused(schema, tmp_path):
    root = variant(tmp_path, "header")
    m = manifest(root)
    m["files"]["stores"]["columns"] = ["store", "geo_known", "city"]
    write_manifest(root, m)
    refused(root, r"parts/stores\.csv\.gz: header .* is not the manifest's columns")


def test_a_manifest_whose_dataset_version_does_not_recompute_is_refused(schema, tmp_path):
    root = variant(tmp_path, "version")
    m = manifest(root)
    m["settings"]["currency"] = "EUR"  # changed after the build: the stored version no longer recomputes
    (root / MANIFEST).write_text(json.dumps(m, indent=1, sort_keys=True), encoding="utf-8")
    refused(root, r"manifest\.json: dataset_version .* does not recompute")


def test_the_deploy_path_refuses_the_local_stand_in_naming_the_provider(schema, tmp_path):
    root = variant(tmp_path, "release")
    points = part_rows("forecast_points", root)
    local = {p["provider"] for p in points}
    assert local == {"local-seasonal-naive"}  # the tiny set's forecast is the local stand-in
    refused(root, rf"refusing provider 'local-seasonal-naive': {len(points)} points", allow_local=False)


def test_a_forecast_missing_one_job_series_day_is_refused_naming_it(schema, tmp_path):
    root = variant(tmp_path, "forecast")
    runs = {r["forecast_run_id"]: r["job"] for r in part_rows("forecast_runs", root)}
    points = part_rows("forecast_points", root)
    drop = next(p for p in points if runs[p["forecast_run_id"]] == "d1" and p["store"] == "S12" and p["sku"] == "P101"
                and p["day"] == "5")
    header = manifest(root)["files"]["forecast_points"]["columns"]
    rewrite_part(root, "forecast_points", header, [[p[c] for c in header] for p in points if p is not drop])
    refused(root, r"no point for job d1, series S12\|P101, day 5")


def test_truth_demand_missing_day_2_of_a_series_is_refused_naming_it(schema, tmp_path):
    root = variant(tmp_path, "truth")
    rows = part_rows("truth_demand", root)
    header = manifest(root)["files"]["truth_demand"]["columns"]
    kept = [[r[c] for c in header] for r in rows if not (r["store"] == "S21" and r["sku"] == "P102" and r["day"] == "2")]
    assert len(kept) == len(rows) - 1
    rewrite_part(root, "truth_demand", header, kept)
    refused(root, r"no hidden demand for series S21\|P102, day 2")


# ------------------------------------------------------------------------------------------------ per-series reads

def test_get_returns_a_public_ref_that_reads_one_series_at_a_time(public_dv):
    ref = refdata.get(public_dv)
    assert isinstance(ref, publicref.PublicRef) and ref.kind == "public"
    assert ref.branches == sorted(r["store"] for r in part_rows("stores"))
    assert sorted(ref.products) == sorted(r["sku"] for r in part_rows("products"))
    run_of = {r["forecast_run_id"]: r for r in part_rows("forecast_runs")}
    d0 = next(i for i, r in run_of.items() if r["job"] == "d0")
    for store, sku in (("S11", "P101"), ("S22", "P102")):
        want = {int(p["day"]): (float(p["low"]), float(p["base"]), float(p["high"]))
                for p in part_rows("forecast_points") if p["forecast_run_id"] == d0 and p["store"] == store
                and p["sku"] == sku}
        assert want and ref.forecast(0, store, sku) == want
        assert {d: ref.provider_of(0, store, sku, d) for d in want} == {d: "local-seasonal-naive" for d in want}
        units = next(float(s["units"]) for s in part_rows("sales") if s["store"] == store and s["sku"] == sku
                     and s["day"] == "-1")
        assert ref.sale(store, sku, -1) == units
    held_out = next(s for s in part_rows("sales") if s["store"] == "S11" and s["sku"] == "P101" and s["day"] == "0")
    assert held_out and ref.sale("S11", "P101", 0) is None  # the eval week is held out (D-15): never a recorded sale
    assert ref.forecast(0, "S11", "P999") == {}
    for origin in ("S11", "S13", "S21"):
        want = [(r["run_code"], r["dest"], int(r["arrives_min"]), int(r["spare_totes"]), r["cold_chain"] == "true",
                 float(r["fixed_cost"])) for r in part_rows("runs") if r["origin"] == origin and r["day"] == "0"]
        got = [(r.run_code, r.dest, r.arrives_min, r.spare_totes, r.cold_chain, r.fixed_cost)
               for r in ref.runs_from(origin, 0)]
        assert sorted(got) == sorted(want)
    assert ref.runs_from("S13", 0) == []  # S13 is the store the fixture's geography leaves unmapped
    for c in part_rows("capacity")[:12]:
        assert ref.free_totes(c["store"], c["zone"], int(c["day"])) == int(c["free_totes"])
    assert ref.free_totes("S11", "chilled", 99) is None


def test_truth_answers_demand_by_product_and_day_and_physical_by_lot(public_dv):
    truth = refdata.truth(public_dv)
    assert isinstance(truth, publicref.PublicTruth)
    want = {r["store"]: float(r["demand"]) for r in part_rows("truth_demand") if r["sku"] == "P101" and r["day"] == "0"}
    assert truth.demand_for("P101", 0) == want and sorted(want) == refdata.get(public_dv).branches
    phys = part_rows("truth_physical")[:5]
    assert truth.physical_of([p["lot_id"] for p in phys] + ["no-such-lot"]) == \
        {p["lot_id"]: float(p["qty"]) for p in phys}
    assert truth.price_response == manifest()["settings"]["truth"]["price_response"]


def test_the_forecast_headline_is_data_forecasts_own_recipe(public_dv):
    """PublicRef counts series-runs from each run's meta, never from the points; data.forecast.provider_summary over
    the points themselves gives the same figures and headline."""
    from data import forecast as F
    runs = [{"forecast_run_id": r["forecast_run_id"], "job": r["job"], "fallbacks": json.loads(r["meta"])["fallbacks"]}
            for r in part_rows("forecast_runs")]
    points = [{"forecast_run_id": p["forecast_run_id"], "branch": p["store"], "sku": p["sku"], "provider": p["provider"]}
              for p in part_rows("forecast_points")]
    assert refdata.get(public_dv).forecast["provider_summary"] == F.provider_summary(runs, points)


# ------------------------------------------------------------------------------------------------ one interface, both kinds

METHODS = ("runs_from", "run_on", "free_totes", "forecast", "provider_of", "sale", "lots0", "inbound0")
TRUTH_METHODS = ("demand_for", "physical_of", "price_response")


def test_the_synthetic_ref_answers_the_shared_methods_from_its_dicts(schema):
    ref, truth = refdata.get(schema), refdata.truth(schema)
    assert ref.kind == "synthetic" and all(hasattr(ref, m) for m in METHODS)
    for origin in ref.branches:
        for day in range(0, ref.calendar["horizon_end"] + 1):
            assert ref.runs_from(origin, day) == [r for r in ref.runs if r.origin == origin and r.day == day]
    for r in ref.runs:
        assert ref.run_on(r.run_code, r.day) == next(x for x in ref.runs if x.run_code == r.run_code and x.day == r.day)
    assert ref.run_on("no-such-run", 0) is None
    for key, n in ref.capacity.items():
        assert ref.free_totes(*key) == n
    for key, days in ref.points.items():
        assert ref.forecast(*key) == days
        assert {d: ref.provider_of(*key, d) for d in days} == {d: ref.point_provider[(*key, d)] for d in days}
    assert ref.forecast(99, ref.branches[0], sorted(ref.products)[0]) == {}
    for key, units in ref.sales.items():
        assert ref.sale(*key) == units
    assert ref.lots0() == ref.seed["lots"] and ref.inbound0() == ref.seed["inbound"]
    some = ref.seed["lots"][0]
    assert ref.lots0(some["branch"], some["sku"]) == [l for l in ref.seed["lots"] if l["branch"] == some["branch"]
                                                       and l["sku"] == some["sku"]]
    # the forecast block is still read as a dict, as before (cases.detail, the refdata tests)
    assert ref.forecast["provider_summary"]["headline"] and ref.forecast["points"]
    # the truth: the dict it was, plus the shared methods
    assert all(hasattr(truth, m) for m in TRUTH_METHODS)
    for (b, sku, day), demand in truth["demand"].items():
        assert truth.demand_for(sku, day)[b] == demand
    assert sum(len(truth.demand_for(s, d)) for s in ref.products for d in {k[2] for k in truth["demand"]}) == \
        len(truth["demand"])
    ids = sorted(truth["physical"])[:5]
    assert truth.physical_of(ids + ["no-such-lot"]) == {i: truth["physical"][i] for i in ids}
    assert truth.price_response == truth["price_response"]


def test_the_public_ref_answers_the_same_methods(public_dv):
    ref, truth = refdata.get(public_dv), refdata.truth(public_dv)
    assert ref.kind == "public" and all(hasattr(ref, m) for m in METHODS)
    assert all(hasattr(truth, m) for m in TRUTH_METHODS)
    run = next(r for r in part_rows("runs") if r["day"] == "2")
    trip = ref.run_on(run["run_code"], 2)
    assert (trip.origin, trip.dest, trip.spare_totes, trip.cancel_cost) == (run["origin"], run["dest"],
                                                                            int(run["spare_totes"]), None)
    lots = ref.lots0("S12", "P101")
    assert [l["lot_id"] for l in lots] == sorted(r["lot_id"] for r in part_rows("lots0") if r["store"] == "S12"
                                                 and r["sku"] == "P101")
    assert all(l["branch"] == "S12" for l in lots)
    assert [i["inbound_id"] for i in ref.inbound0("S11", "P102")] == \
        sorted(r["inbound_id"] for r in part_rows("inbound0") if r["store"] == "S11" and r["sku"] == "P102")
    with pytest.raises(ValueError, match="one series"):
        ref.lots0()  # a public set is never read whole
    with pytest.raises(ValueError, match="one series"):
        ref.inbound0(sku="P101")


# ------------------------------------------------------------------------------------------------ no full read (D-11)

LARGE = ("ref_sales_day", "ref_forecast_point", "ref_truth_demand", "ref_lot0", "ref_inbound0")
BOUND = re.compile(r"\b(?:\w+\.)?(store|sku)\s*(=|=\s*ANY\s*\()\s*(%s|%\(\w+\)s|\$\d+|:\w+)", re.I)


def test_the_api_never_reads_a_large_reference_table_whole(public_dv):
    """Build the tiny public template's day-0 worlds (init_worlds and the day start), screen its lots (flags, 05.2-15),
    open one flag (an EngineInput and an evaluation), read the board and a case detail, re-evaluate a case and step
    one day, recording every statement the API sends. Each SELECT that reads a large reference table either is part
    of an INSERT ... SELECT (a set-based copy inside the database) or binds a store or a product (one series)."""
    from api import advance, cases, flags, inputs, sandbox
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    refdata.get.cache_clear()  # a cold reference load is captured too
    refdata.truth.cache_clear()
    run_id = f"tpl-pub-{uuid.uuid4().hex[:8]}"
    sa.event.listen(db.engine(), "before_cursor_execute", record)
    try:
        with db.tx() as conn:  # a scratch TEMPLATE, never ACTIVE: no workspace is ever cloned from it
            db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) VALUES "
                         "(:r, 'TEMPLATE', :v, 'INITIALIZING', 0, 1)", r=run_id, v=public_dv)
            ref, truth = refdata.get(public_dv), refdata.truth(public_dv)
            sandbox.init_worlds(conn, run_id, ref, truth)
            run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)
            assert advance.day_start(conn, run) > 0  # day 0's 06:00 deliveries became lots
            restore_the_designed_flag(conn, run)  # test-local: the designed lot qualifies again (quick 261017-ofn)
            assert cases.open_cases(conn, run) == []  # the public set flags (05.2-15 D-23): no case, no search
            flagged = [r["key"] for r in db.rows(conn, "SELECT key FROM case_flags WHERE run_id = :r ORDER BY key",
                                                 r=run_id)]
            assert flagged, "the tiny set flags at least one lot on day 0"
            assert {c["key"] for c in cases.board_page(conn, run)["cases"]} == set(flagged)
            opened = [flags.open_from_flag(conn, run, flagged[0])["case_id"]]  # evaluate on open (D-25)
            detail = cases.detail(conn, run, opened[0])
            assert detail["versions"]["forecast_headline"] and detail["engine_input"]
            lot = detail["lot"]["lot_id"]
            inp = inputs.build_input(conn, run, lot)
            assert inp.forecasts and inp.runs is not None
            cases.evaluate_case(conn, run, cases.case_row(conn, run_id, opened[0]))
            sandbox.network_check(conn, run, ref, truth)
            sandbox.limits_check(conn, run, ref)
            advance.step_world(conn, run, "do_nothing", truth)  # hidden demand, per product and day
    finally:
        sa.event.remove(db.engine(), "before_cursor_execute", record)
        with db.tx() as conn:
            db.run(conn, "UPDATE demo_runs SET status = 'FAILED', failure = 'test scratch' WHERE run_id = :r",
                   r=run_id)
    reads = [s for s in seen if re.search(r"\bSELECT\b", s, re.I) and any(re.search(rf"\b{t}\b", s) for t in LARGE)]
    read_tables = {t for s in reads for t in LARGE if re.search(rf"\b{t}\b", s)}
    assert {"ref_sales_day", "ref_forecast_point", "ref_truth_demand"} <= read_tables, read_tables  # not vacuous
    whole = [s for s in reads if not re.match(r"\s*INSERT\b", s, re.I) and not BOUND.search(s)]
    assert whole == [], whole
