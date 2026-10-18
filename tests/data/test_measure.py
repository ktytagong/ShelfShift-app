"""The 05.2-05 measurement spike end to end on the synthetic fixture (05.2 D-21, D-23, D-28; DATA-15, DATA-17).

The rows come from tests/data/fixtures/frn_sample_synthetic.csv (a synthetic test fixture in the FreshRetailNet-50K
schema; not FreshRetailNet rows; sale_amount = units x the generator's STEP). Every parameter below is a test value,
not a reference or product value: deliveries are oversized (multiplier 1.6..1.8) so that Keep wastes and lots
qualify, the service limit is wide enough to link the fixture's stores, and store "13" is unmapped (the fixture's
documented unmapped store). Counts are compared with direct calls of the generators and the screen, never with a
pasted number.
"""
import gzip
import importlib.util
import json
import pathlib

import pytest

from api import screen
from data import config as C
from data import measure as M
from data import public as U
from data import public_config as P
from data import stock as K

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
CSV, SIDECAR = FIX / "frn_sample_synthetic.csv", FIX / "frn_sample_synthetic.json"


def _step() -> float:
    spec = importlib.util.spec_from_file_location("make_frn_sample", FIX / "make_frn_sample.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    return gen.STEP


def _set(product: dict, shelf: int, cycle: int, case: int, schedule) -> dict:
    return {"units_factor": 1 / _step(), "product": product, "schedule": schedule, "shelf_life_days": shelf,
            "cycle_days": cycle, "order_case": case, "multiplier": [1.6, 1.8], "lookback_days": 28,
            "same_weekday_obs": 4, "error_share": 0.3, "error_range": [0.0, 0.5], "error_from_day": -15,
            "expiry_estimated_share": 0.2, "topup_on_eval_days": False, "k": 2}


def test_params() -> dict:
    cfg = C.load()
    return {
        "seed": 7,
        "sets": {"short": _set({"zone": "chilled", "price": 2.0, "disposal_cost": 0.1, "units_per_tote": 4,
                                "kg_per_unit": 0.5}, 3, 2, 4, [[1, 20], [0, 30]]),
                 "long": _set({"zone": "ambient", "price": 1.0, "disposal_cost": 0.05, "units_per_tote": 6,
                               "kg_per_unit": 1.0}, 6, 3, 6, [[2, 20], [1, 30]])},
        "network": {"city_radius_km": 15.0, "speed_kmh": 30.0, "handling_min": 15, "max_travel_min": 120,
                    "run_days": list(range(0, 7)), "loading_cutoff_min": 720, "departs_min": 750, "spare_totes": 15,
                    "cold_chain_share": 0.5, "fixed_cost_base": 1.0, "per_km_cost": 0.1, "per_unit_cost": 0.0,
                    "cancel_cost": None, "capacity_totes": {"chilled": 20, "ambient": 25}, "unmapped": ["13"]},
        "policy": cfg["policy"]["engine_policy"],
        "markdown_response": cfg["policy"]["markdown_response"],
        "case_opening": cfg["world"]["case_opening"],
        "warmup_response": {"20": 1.0, "30": 1.0},
        "latency": {"set": "short", "ks": [1, 2], "steps": [0, 10], "top": 2, "reps": 1},
        "catalogue": {"set": "short", "k": 2, "step": 10, "n": 200},
    }


test_params.__test__ = False  # a helper, not a test


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("measure")
    params_path, out = tmp / "params.json", tmp / "measure.json"
    params_path.write_text(json.dumps(test_params()))
    for step in ("stock", "screen", "latency", "catalogue"):
        assert M.main(["measure", step, "--csv", str(CSV), "--fixture", str(SIDECAR), "--params", str(params_path),
                       "--out", str(out)]) == 0
    return json.loads(out.read_text()), tmp / "measure-state", test_params()


@pytest.fixture(scope="module")
def direct():
    """{set: [(key, SeriesStock)]} from direct simulate calls on the fixture series."""
    cfg = P.load_for(SIDECAR)
    params = test_params()
    out = {}
    for name in params["sets"]:
        sc = M.set_cfg(params, name, cfg["calendar"])
        rows_by = list(U.series_iter(None, cfg, csv_path=CSV))
        sims = []
        for key, rows in rows_by:
            sp = M.dataclasses.replace(sc.stock, product=M.dataclasses.replace(sc.product, sku=key[2]))
            sims.append((key, K.simulate(key[1], key[2], [r["day"] for r in rows],
                                         [round(r["sale_amount"] * sc.factor, M.UNIT_PLACES) for r in rows],
                                         [r["stockout"] for r in rows], sp)))
        out[name] = sims
    return out


def test_series_iter_yields_every_series_once_in_code_order():
    cfg = P.load_for(SIDECAR)
    series = list(U.series_iter(None, cfg, csv_path=CSV))
    keys = [(k[1], k[2]) for k, _ in series]
    assert len(keys) == len(set(keys)) == 10
    assert keys == sorted(keys, key=lambda k: (int(k[0]), int(k[1])))
    for (city, store, product, cats), rows in series:
        assert [r["day"] for r in rows] == list(range(cfg["calendar"]["first_day"], cfg["calendar"]["last_day"] + 1))
        assert {(r["city"], r["store"], r["product"]) for r in rows} == {(city, store, product)}
        assert len(cats) == 4


def test_group_series_refuses_unsorted_rows():
    rows = [{"city": "1", "store": s, "product": p, "mgmt": "1", "cat1": "1", "cat2": "1", "cat3": "1", "day": d}
            for s, p, d in (("11", "101", 0), ("11", "102", 0), ("11", "101", 1))]
    with pytest.raises(U.PublicDataError):
        list(U.group_series(rows))


def test_stock_counts_equal_direct_simulate_calls(run, direct):
    report, _, params = run
    for name, sims in direct.items():
        block = report["stock"][name]
        assert block["series"] == len(sims)
        assert block["lots_day0"]["total"] == sum(len(st.lots_day0) for _, st in sims)
        assert block["topup"]["units"] == round(sum(st.counters["topup_units"] for _, st in sims), 2)
        assert block["topup"]["eval_day_units"] == round(sum(r["qty"] for _, st in sims for r in st.eval_topups), 2)
        assert block["record_errors"]["error_lots"] == sum(st.counters["error_lots"] for _, st in sims)
        assert block["peak_rss_mb"] > 0 and block["wall_s"] >= 0


def test_the_state_at_the_open_follows_the_warm_up(run, direct):
    """Each series' open-day lots are its day-0 lots and inbound of days 0..open after the warm-up's sales: a unit
    received is sold, wasted, lost to nothing, or still on hand (the record never below the physical stock)."""
    _, state_dir, params = run
    cfg = P.load_for(SIDECAR)
    open_day = cfg["calendar"]["demo_open_day"]
    state = M.load_state(state_dir / "state-short.jsonl.gz")
    for key, st in direct["short"]:
        s = state[(key[1], key[2])]
        received = (sum(r["physical"] for r in st.lots_day0)
                    + sum(r["physical"] for r in st.inbound if r["arrival_day"] <= open_day))
        sold = sum(o[2] for o in s.outcomes)
        wasted = sum(o[3] for o in s.outcomes)
        on_hand = sum(l[2] for l in s.lots)
        assert received == pytest.approx(sold + wasted + on_hand, abs=1e-6)
        assert all(l[1] >= l[2] - 1e-9 for l in s.lots)
        assert {i[0] for i in s.later} == {r["lot_id"] for r in st.inbound if r["arrival_day"] > open_day}


def test_screen_counts_every_lot_in_its_window_and_equals_a_direct_screen(run):
    report, state_dir, params = run
    cfg = P.load_for(SIDECAR)
    cal = cfg["calendar"]
    for name in params["sets"]:
        block = report["screen"][name]
        sc = M.set_cfg(params, name, cal)
        state = M.load_state(state_dir / f"state-{name}.jsonl.gz")
        before = params["case_opening"]["days_before_first_step"]
        in_window = [(s, l) for s in state.values() for l in s.lots
                     if screen.window(l[3], sc.schedule, before)[0] <= cal["demo_open_day"]
                     <= screen.window(l[3], sc.schedule, before)[1]]
        assert block["lots_open"] == sum(len(s.lots) for s in state.values())
        assert block["in_window"] == block["screened"] == len(in_window)
        ctx = M.Ctx(params, cal, sc, state, sc.k)
        direct = sorted([s.city, s.store, s.product, l[0]] for s, l in in_window
                        if screen.screen(ctx.case_input(s.store, s.product, l[0])[0],
                                         params["case_opening"])["qualifies"])
        assert block["total"] == len(direct) == sum(block["by_city"].values())
        assert sorted(M.load_qualifying(state_dir, name), key=M.code_order) == \
            sorted(map(tuple, direct), key=M.code_order)
        assert block["by_store"]["stores"] == len({s.store for s in state.values()})
        assert block["by_store"]["max"] == max([0] + [t["lots"] for t in block["by_store"]["top"]])
    assert report["screen"]["short"]["total"] > 0  # the oversized test deliveries make Keep waste


def test_an_unmapped_store_gets_no_run_and_a_destination_must_carry_the_product(run):
    _, state_dir, params = run
    cfg = P.load_for(SIDECAR)
    sc = M.set_cfg(params, "short", cfg["calendar"])
    state = M.load_state(state_dir / "state-short.jsonl.gz")
    ctx = M.Ctx(params, cfg["calendar"], sc, state, 2)
    assert "13" in ctx.unmapped and "13" not in ctx.runs_by_origin
    assert all(r.dest != "13" for runs in ctx.runs_by_origin.values() for r in runs)
    for s in state.values():
        if s.lots:
            inp, info = ctx.case_input(s.store, s.product, s.lots[0][0])
            assert all((r.dest, s.product) in state for r in inp.runs)
            assert {l.branch for l in inp.lots} <= {s.store} | {r.dest for r in inp.runs}
            assert info["runs"] == len(inp.runs) <= info["runs_network"] <= 2


def test_latency_and_catalogue_report_every_cell_and_every_kind(run):
    report, _, params = run
    lat = report["latency"]
    keys = {(r["k"], r["shortlist_step"]) for r in lat["rows"]}
    assert keys == {(k, s) for k in params["latency"]["ks"] for s in params["latency"]["steps"]}
    assert all(r["slowest_median_s"] > 0 and r["cases"] == min(2, lat["qualifying"]) for r in lat["rows"])
    assert lat["set"] == params["latency"]["set"]
    assert {(s["set"], s["rule"]) for s in lat["supplementary"]} == \
        {(n, r) for n in params["sets"] for r in ("destinations", "quantity")} - {(lat["set"], "destinations")}
    for sup in lat["supplementary"]:
        assert {(r["k"], r["shortlist_step"]) for r in sup["rows"]} == keys
    cat = report["catalogue"]
    assert cat["evaluated"] == min(params["catalogue"]["n"], cat["qualifying"])
    assert all(isinstance(cat["kinds"][k], int) for k in M.KINDS)
    assert sum(cat["kinds"][k] for k in ("transfer", "markdown", "keep")) == cat["evaluated"]
    assert cat["kinds"]["transfers_unavailable"] == 0 or "13" in params["network"]["unmapped"]
    assert [b["set"] for b in cat["supplementary"]] == [n for n in params["sets"] if n != cat["set"]]
    for b in cat["supplementary"]:
        assert b["evaluated"] == min(params["catalogue"]["n"], b["qualifying"])
        assert sum(b["kinds"][k] for k in ("transfer", "markdown", "keep")) == b["evaluated"]


def test_the_db_step_refuses_any_database_but_a_scratch_test_one():
    for url in ("postgresql+pg8000://postgres@localhost:5432/shelfshift",
                "postgresql+pg8000://postgres@localhost:5432/shelfshift_test",
                "postgresql+pg8000://postgres@localhost:5432/shelfshift_web_test",
                "postgresql+pg8000://postgres@localhost:5432/measure"):
        with pytest.raises(ValueError):
            M.scratch_url(url)
    assert M.scratch_url("postgresql+pg8000://postgres@localhost:5432/shelfshift_052_measure_test").database \
        == "shelfshift_052_measure_test"


def test_the_measure_module_never_imports_the_database_layer():
    import ast

    tree = ast.parse(pathlib.Path(M.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported |= {node.module} | {f"{node.module}.{a.name}" for a in node.names}
    assert "api.screen" in imported
    assert not imported & {"api.db", "api.app", "api.refdata", "api.inputs", "api.evaluate"}
