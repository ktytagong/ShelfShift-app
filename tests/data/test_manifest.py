"""The public artifact set: manifest, versions, coverage and release checks, the tiny set (05.2-10; spec §4 Data flow 1,
§5 Public dataset and Horizon coverage; DATA-05 kept, DATA-08, DATA-13; 05.2-CONTEXT D-08, D-14, D-15).

The set is built from tests/data/fixtures/frn_sample_synthetic.csv (a synthetic test fixture in the FreshRetailNet-50K
schema; not FreshRetailNet rows) with its override file, through every stage: stock, network, the local stand-in
forecast, then the manifest. 10 series (5 stores x 2 products), days -42..6. The fixture's own geography leaves store
"13" unmapped and its designed example is store "12", product "101" (test data only; the release reference sets none).

Hand counts used below (the fixture calendar: last_demo_day 6; the longest shelf life 3, so horizon_end = 6 + 3 = 9;
forecast.runs "d0..d3 plus the backtest", backtest_days 14):
- runs d0..d3 forecast days n..9: 10 + 9 + 8 + 7 = 34 points per series, the backtest 14 more: 48 per series, so 480
  points over the 10 series;
- hidden demand covers days 0..6 for every series (D-15): 10 x 7 = 70 rows.
"""
import copy
import csv
import gzip
import importlib.util
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

from data import config as C
from data import manifest as M
from data import public as U
from data import public_build as B
from data import public_config as P
from data import public_forecast as PF
from data import versions as V

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
CSV, SIDECAR = FIX / "frn_sample_synthetic.csv", FIX / "frn_sample_synthetic.json"
TINY = FIX / "public_tiny"
REF = ROOT / "reference"
EXPECTED_TABLES = {
    "sales": "ref_sales_day", "weather": "ref_weather_day", "products": "ref_product", "stores": "ref_store",
    "lots0": "ref_lot0", "inbound0": "ref_inbound0", "runs": "ref_run", "capacity": "ref_capacity",
    "forecast_runs": "ref_forecast_run", "forecast_points": "ref_forecast_point",
    "truth_physical": "ref_truth_physical", "truth_demand": "ref_truth_demand"}
TRUTH_PARTS = {"truth_physical", "truth_demand"}
N_SERIES, POINTS_PER_SERIES, LAST_DEMO_DAY, HORIZON_END = 10, 48, 6, 9
MAX_TINY_BYTES = 2 * 1024 * 1024  # the plan's ceiling for a committed test set


# --------------------------------------------------------------------------------------------------- helpers

def build(out: pathlib.Path, reports: pathlib.Path) -> int:
    return B.main(["build", "--csv", str(CSV), "--fixture", str(SIDECAR), "--out", str(out), "--forecast", "local",
                   "--reports", str(reports)])


def files_of(root: pathlib.Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def read_part(path: pathlib.Path) -> tuple[list[str], list[list[str]]]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        return next(reader), list(reader)


def write_part(path: pathlib.Path, header: list[str], rows: list[list[str]]) -> None:
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        text = io.TextIOWrapper(gz, encoding="utf-8", newline="")
        w = csv.writer(text, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
        text.flush()
        text.detach()
    path.write_bytes(raw.getvalue())


def edit_part(root: pathlib.Path, part: str, change) -> None:
    """Rewrite one part's rows with `change(header, rows) -> rows`."""
    path = root / "parts" / f"{part}.csv.gz"
    header, rows = read_part(path)
    write_part(path, header, change(header, rows))


def manifest_of(root: pathlib.Path) -> dict:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def restamp(root: pathlib.Path, m: dict) -> None:
    """Write `m` with a dataset_version that recomputes, so only the edit itself can be refused."""
    m = {k: v for k, v in m.items() if k != "dataset_version"}
    m["dataset_version"] = M.dataset_version(m)
    (root / "manifest.json").write_text(json.dumps(m, indent=1, sort_keys=True) + "\n", encoding="utf-8")


@pytest.fixture(scope="module")
def cfg():
    return P.load_for(SIDECAR)


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> pathlib.Path:
    root = tmp_path_factory.mktemp("set")
    assert build(root / "out", root / "reports") == 0
    return root / "out"


@pytest.fixture
def copy_of(built, tmp_path):
    def make() -> pathlib.Path:
        dst = tmp_path / "copy"
        shutil.copytree(built, dst)
        return dst
    return make


def remanifest(root: pathlib.Path, cfg: dict) -> dict:
    return M.write_manifest(root, cfg, fixture=SIDECAR)


# --------------------------------------------------------------------------------------------------- the format

def test_tables_map_every_part_to_exactly_one_table_and_flag_the_truth_parts():
    assert M.TABLES == EXPECTED_TABLES
    assert len(set(M.TABLES.values())) == len(M.TABLES)
    assert set(M.TRUTH) == TRUTH_PARTS


def test_the_build_writes_every_part_and_a_manifest_that_checks(built):
    m = manifest_of(built)
    assert m["kind"] == "public"
    assert set(m["files"]) == set(EXPECTED_TABLES)
    for part, entry in m["files"].items():
        header, rows = read_part(built / entry["path"])
        assert entry["table"] == EXPECTED_TABLES[part], part
        assert entry["truth"] is (part in TRUTH_PARTS), part
        assert entry["columns"] == header, part
        assert entry["rows"] == len(rows), part
        assert entry["sha256"] == U.sha256_of(built / entry["path"]), part
    assert M.check(built) == m["dataset_version"]


def test_the_dataset_version_is_ds_plus_12_hex_over_the_manifest_without_it(built):
    m = manifest_of(built)
    body = {k: v for k, v in m.items() if k != "dataset_version"}
    assert re.fullmatch(r"ds-[0-9a-f]{12}", m["dataset_version"])
    assert m["dataset_version"] == V.digest("ds-", body) == M.dataset_version(body)
    assert re.fullmatch(r"in-[0-9a-f]{12}", m["input_version"])


def test_the_dataset_version_is_stable_across_two_builds(built, tmp_path):
    assert build(tmp_path / "again", tmp_path / "reports") == 0
    assert (tmp_path / "again" / "manifest.json").read_bytes() == (built / "manifest.json").read_bytes()


def test_the_manifest_carries_the_set_settings(built, cfg):
    s = manifest_of(built)["settings"]
    for key in ("calendar", "mark", "currency", "policy", "markdown_response", "case_opening", "evaluation", "board",
                "catalogue", "series", "truth", "network", "attribution", "horizon_end"):
        assert key in s, key
    assert s["calendar"]["last_demo_day"] == LAST_DEMO_DAY and s["horizon_end"] == HORIZON_END
    assert s["truth"]["price_response"] == cfg["truth"]["price_response"]
    assert s["policy"]["shortlist_step"] == cfg["policy"]["shortlist_step"]
    assert s["attribution"]["creator"] == cfg["dataset"]["creator"]
    assert s["attribution"]["license"] == cfg["dataset"]["license"]
    assert s["network"]["unmapped"] == ["S13"]  # the fixture's unmapped store (D-31), excluded from transfers
    assert s["designed"]["label"] == "Designed example (test fixture)"


def test_the_forecast_block_names_the_provider_and_counts_the_points(built):
    f = manifest_of(built)["forecast"]
    assert f["providers"] == {"local-seasonal-naive": N_SERIES * POINTS_PER_SERIES}
    assert f["stand_in"] is True and f["fallbacks"] == 0
    assert [r["job"] for r in f["runs"]] == ["d0", "d1", "d2", "d3", "backtest"]


def test_the_reference_history_id_recomputes_from_the_manifest(built, cfg):
    m = manifest_of(built)
    assert M.history_id(m) == PF.reference_history_id(cfg)
    assert {r["reference_history_id"] for r in m["forecast"]["runs"]} == {PF.reference_history_id(cfg)}


# --------------------------------------------------------------------------------------------------- check

def test_a_manifest_whose_dataset_version_does_not_recompute_is_refused(copy_of):
    root = copy_of()
    m = manifest_of(root)
    m["settings"]["board"]["page_size"] += 1  # edited without a new dataset_version
    (root / "manifest.json").write_text(json.dumps(m, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(M.ManifestError, match="dataset_version"):
        M.check(root)


@pytest.mark.parametrize("part", sorted(EXPECTED_TABLES))
def test_one_changed_byte_in_any_part_is_refused_naming_the_part(copy_of, part):
    root = copy_of()
    path = root / "parts" / f"{part}.csv.gz"
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))
    with pytest.raises(M.ManifestError, match=re.escape(f"parts/{part}.csv.gz")):
        M.check(root)


def test_a_row_count_that_differs_from_the_manifest_is_refused(copy_of):
    root = copy_of()
    m = manifest_of(root)
    m["files"]["weather"]["rows"] += 1
    restamp(root, m)
    with pytest.raises(M.ManifestError, match=r"parts/weather\.csv\.gz.*rows"):
        M.check(root)


def test_a_dropped_forecast_row_is_refused_naming_job_series_and_day(copy_of, cfg):
    root = copy_of()
    _, runs = read_part(root / "parts" / "forecast_runs.csv.gz")
    d1 = next(r[0] for r in runs if r[1] == "d1")
    edit_part(root, "forecast_points", lambda h, rows: [r for r in rows if r[:4] != [d1, "S11", "P101", "5"]])
    remanifest(root, cfg)
    with pytest.raises(M.ManifestError, match=r"forecast_points.*d1.*S11.*P101.*day 5"):
        M.check(root)


def test_truth_demand_missing_day_2_of_a_series_is_refused_naming_it(copy_of, cfg):
    root = copy_of()
    edit_part(root, "truth_demand", lambda h, rows: [r for r in rows if r[:3] != ["S12", "P102", "2"]])
    remanifest(root, cfg)
    with pytest.raises(M.ManifestError, match=r"truth_demand.*S12.*P102.*day 2"):
        M.check(root)


def test_truth_demand_is_required_only_through_last_demo_day(built):
    # D-15: hidden demand is the public eval week, days 0..last_demo_day; forecasts still reach horizon_end
    _, rows = read_part(built / "parts" / "truth_demand.csv.gz")
    assert len(rows) == N_SERIES * (LAST_DEMO_DAY + 1)
    assert max(int(r[2]) for r in rows) == LAST_DEMO_DAY < HORIZON_END
    _, points = read_part(built / "parts" / "forecast_points.csv.gz")
    assert max(int(r[3]) for r in points) == HORIZON_END


def test_a_lot_expiring_after_the_horizon_end_is_refused(copy_of, cfg):
    root = copy_of()

    def late(header, rows):
        rows[0][header.index("expiry_day")] = str(HORIZON_END + 1)
        return rows
    edit_part(root, "lots0", late)
    remanifest(root, cfg)
    with pytest.raises(M.ManifestError, match=r"lots0.*expires.*day 10.*horizon_end 9"):
        M.check(root)


def test_a_delivery_expiring_after_the_horizon_end_is_refused(copy_of, cfg):
    root = copy_of()

    def late(header, rows):
        rows[-1][header.index("expiry_day")] = str(HORIZON_END + 2)
        return rows
    edit_part(root, "inbound0", late)
    remanifest(root, cfg)
    with pytest.raises(M.ManifestError, match=r"inbound0.*expires.*day 11.*horizon_end 9"):
        M.check(root)


def test_missing_capacity_for_a_store_day_is_refused(copy_of, cfg):
    root = copy_of()
    edit_part(root, "capacity", lambda h, rows: [r for r in rows if not (r[0] == "S21" and r[2] == "7")])
    remanifest(root, cfg)
    with pytest.raises(M.ManifestError, match=r"capacity.*S21.*day 7"):
        M.check(root)


# --------------------------------------------------------------------------------------------------- release check

def test_the_release_check_refuses_the_stand_in_naming_the_provider_and_the_points(built):
    with pytest.raises(M.ManifestError) as e:
        M.release_check(built)
    n = N_SERIES * POINTS_PER_SERIES
    assert "local-seasonal-naive" in str(e.value) and f"{n} points" in str(e.value)


def to_ai_forecast(root: pathlib.Path, cfg: dict) -> None:
    def rewrite(header, rows):
        i = header.index("provider")
        for r in rows:
            r[i] = "ai_forecast"
        return rows
    edit_part(root, "forecast_points", rewrite)
    remanifest(root, cfg)


def test_every_point_from_bigquery_and_the_manifest_rewritten_passes(copy_of, cfg):
    root = copy_of()
    to_ai_forecast(root, cfg)
    info = M.release_check(root)
    assert info["dataset_version"] == manifest_of(root)["dataset_version"] == M.check(root)
    assert manifest_of(root)["forecast"]["providers"] == {"ai_forecast": N_SERIES * POINTS_PER_SERIES}
    assert manifest_of(root)["forecast"]["stand_in"] is False
    assert "test fixture" in info["note"]  # a fixture-built set says so, it is not the public dataset


def test_a_forecast_built_on_other_inputs_is_refused(copy_of, cfg):
    root = copy_of()
    to_ai_forecast(root, cfg)

    def other(header, rows):
        i = header.index("meta")
        meta = json.loads(rows[0][i])
        meta["reference_history_id"] = "fb-000000000000"
        rows[0][i] = C.canonical(meta)
        return rows
    edit_part(root, "forecast_runs", other)
    remanifest(root, cfg)
    with pytest.raises(M.ManifestError, match="reference_history_id"):
        M.release_check(root)


def test_a_manifest_whose_input_version_does_not_recompute_is_refused(copy_of, cfg):
    root = copy_of()
    to_ai_forecast(root, cfg)
    m = manifest_of(root)
    m["input_version"] = "in-000000000000"
    restamp(root, m)
    assert M.check(root)  # the plain check does not need the inputs
    with pytest.raises(M.ManifestError, match="input_version"):
        M.release_check(root)


def test_another_provider_is_refused(copy_of, cfg):
    root = copy_of()

    def rewrite(header, rows):
        i = header.index("provider")
        for r in rows:
            r[i] = "ai_forecast"
        rows[3][i] = "hand-typed"
        return rows
    edit_part(root, "forecast_points", rewrite)
    remanifest(root, cfg)
    with pytest.raises(M.ManifestError, match=r"'hand-typed'.*1 point"):
        M.release_check(root)


def test_check_prints_ok_and_the_release_check_exits_1_on_the_stand_in(built, capsys):
    assert M.main(["check", str(built)]) == 0
    assert capsys.readouterr().out.startswith(f"ok {manifest_of(built)['dataset_version']}")
    assert M.main(["check", str(built), "--release"]) == 1
    assert "local-seasonal-naive" in capsys.readouterr().err


# --------------------------------------------------------------------------------------------------- the tiny set

def test_the_tiny_set_is_the_writer_output(tmp_path):
    assert build(tmp_path / "tiny", tmp_path / "reports") == 0
    assert files_of(tmp_path / "tiny") == files_of(TINY)


def test_the_tiny_set_is_small_and_checks_ok_and_is_refused_for_release():
    assert sum(len(b) for b in files_of(TINY).values()) < MAX_TINY_BYTES
    assert M.check(TINY) == manifest_of(TINY)["dataset_version"]
    with pytest.raises(M.ManifestError, match="local-seasonal-naive"):
        M.release_check(TINY)


def test_the_tiny_set_keeps_the_unmapped_store_and_the_designed_example(cfg):
    _, stores = read_part(TINY / "parts" / "stores.csv.gz")
    assert {r[0]: r[2] for r in stores}["S13"] == "false"
    _, runs = read_part(TINY / "parts" / "runs.csv.gz")
    assert not [r for r in runs if "S13" in (r[2], r[3])]
    header, lots = read_part(TINY / "parts" / "lots0.csv.gz")
    designed = [r for r in lots if r[header.index("tag")] == "designed"]
    assert designed and {(r[1], r[2]) for r in designed} == {("S12", "P101")}


def test_frn_check_on_the_tiny_set_runs_in_the_app_environment():
    done = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "frn.py"), "check", str(TINY)],
                          capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert done.returncode == 0, done.stderr
    assert re.match(r"ok ds-[0-9a-f]{12}", done.stdout)


# --------------------------------------------------------------------------------------------------- reference

def test_the_public_fingerprint_covers_the_four_reference_files(tmp_path):
    fp = P.fingerprint()
    assert re.fullmatch(r"ref-[0-9a-f]{12}", fp)
    for name in ("public.json", "categories.json", "geography.json", "policy.json"):
        shutil.copy(REF / name, tmp_path / name)
    assert P.fingerprint(tmp_path) == fp
    geo = json.loads((tmp_path / "geography.json").read_text(encoding="utf-8"))
    (tmp_path / "geography.json").write_text(json.dumps(geo, indent=4, sort_keys=True), encoding="utf-8")
    assert P.fingerprint(tmp_path) == fp  # layout and key order do not count
    geo["runs"]["k"]["value"] += 1
    (tmp_path / "geography.json").write_text(json.dumps(geo), encoding="utf-8")
    assert P.fingerprint(tmp_path) != fp


def test_the_budget_cap_is_a_policy_leaf_left_out_of_the_fingerprint(tmp_path):
    """The per-query byte cap (user 2026-10-17) guards the cost of the BigQuery run; it is not an input of the set, so
    changing it changes neither the fingerprint nor the forecast basis, and no set needs a rebuild for it."""
    tree = json.loads((REF / "public.json").read_text(encoding="utf-8"))
    leaf = tree["budget"]["max_bytes_billed"]
    assert leaf["type"] == "Policy" and leaf["source"] == "user 2026-10-17: the budget limit is enforced on every paid query"
    assert isinstance(leaf["value"], int) and leaf["value"] > 0
    for name in ("public.json", "categories.json", "geography.json", "policy.json"):
        shutil.copy(REF / name, tmp_path / name)
    fp, history = P.fingerprint(tmp_path), PF.reference_history_id(P.load())
    leaf["value"] *= 2
    (tmp_path / "public.json").write_text(json.dumps(tree), encoding="utf-8")
    assert P.fingerprint(tmp_path) == fp
    assert PF.reference_history_id(P.load(tmp_path, profile=None)) == history
    tree["forecast"]["dataset"]["source"] += "."  # any other block still counts
    (tmp_path / "public.json").write_text(json.dumps(tree), encoding="utf-8")
    assert P.fingerprint(tmp_path) != fp


def test_the_fixture_override_sets_its_geography_units_and_designed_example(cfg):
    spec = importlib.util.spec_from_file_location("make_frn_sample", FIX / "make_frn_sample.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    assert cfg["geography"]["coverage"]["unmapped"] == ["13"]
    assert cfg["units"]["factor"] == round(1 / gen.STEP)
    assert cfg["designed"]["verify_example"]["store"] == "12"
    assert cfg["designed"]["verify_example"]["product"] == "101"
    ref = P.load()
    assert "designed" not in ref and ref["geography"]["coverage"]["unmapped"] == []  # test data only


def test_a_fixture_override_of_a_geography_path_the_reference_lacks_is_refused(tmp_path):
    tree = json.loads(SIDECAR.read_text(encoding="utf-8"))
    tree["geography"]["service"]["radius_km"] = copy.deepcopy(tree["geography"]["service"]["max_travel_min"])
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(tree), encoding="utf-8")
    with pytest.raises(C.ConfigError, match=r"geography\.service\.radius_km"):
        P.load_for(bad)


# --------------------------------------------------------------------------------------------------- the BigQuery rehearsal (05.2-11)
# The user's run (D-22) rehearsed with test_bq_sql's fake client: `frn.py forecast --provider ai_forecast` over the
# input part already in <out>/parts, then `frn.py build --forecast keep`. Hand counts: the spoiled series d1|S21|P102
# is forecast for run d1's days 1..9, so 9 of the 480 points come from ARIMA_PLUS and 471 from ai_forecast; run d1
# holds 10 series (5 stores x 2 products), 9 answered by ai_forecast and 1 by arima_plus.

from datetime import date

from data import bq
from data import forecast as F
from tests.data.test_bq_sql import PROJECT as FAKE_PROJECT  # a well-formed id that is no real project
from tests.data.test_bq_sql import FakeBigQuery, PublicFake

SPOILED = "d1|S21|P102"  # its first ai_forecast row has a crossed interval, so it alone falls back
SPOILED_POINTS = 9  # run d1 forecasts days 1..horizon_end 9


def keep(out: pathlib.Path, reports: pathlib.Path) -> int:
    return B.main(["build", "--csv", str(CSV), "--fixture", str(SIDECAR), "--out", str(out), "--forecast", "keep",
                   "--reports", str(reports)])


def fake_bigquery_run(monkeypatch, out: pathlib.Path, cfg: dict, spoil=()) -> int:
    """The user's paid command, with the fake client: no google module, no network."""
    client = PublicFake(date.fromisoformat(cfg["calendar"]["day0"]), spoil_ai=set(spoil))
    monkeypatch.setattr(bq, "_bigquery", lambda: FakeBigQuery)
    monkeypatch.setattr(bq, "make_client", lambda project, location: client)
    return PF.main(["forecast", "--provider", "ai_forecast", "--project", FAKE_PROJECT, "--out", str(out),
                    "--fixture", str(SIDECAR)])


def no_provider(monkeypatch) -> None:
    monkeypatch.setattr(PF, "stream_forecast", lambda *a, **k: pytest.fail("keep must call no forecast provider"))
    monkeypatch.setattr(PF, "prepare_input", lambda *a, **k: pytest.fail("keep must not rebuild the input part"))


def test_the_bigquery_run_then_build_keep_passes_the_release_check(built, copy_of, cfg, monkeypatch, tmp_path,
                                                                   capsys):
    root = copy_of()
    assert fake_bigquery_run(monkeypatch, root, cfg, spoil={SPOILED}) == 0
    assert capsys.readouterr().out.startswith("forecast: 5 runs, 480 points;")
    with pytest.raises(M.ManifestError, match="forecast_(runs|points)"):
        M.check(root)  # the old manifest still describes the stand-in's bytes until keep rewrites it
    points = (root / "parts" / "forecast_points.csv.gz").read_bytes()

    no_provider(monkeypatch)
    assert keep(root, tmp_path / "reports") == 0
    out = capsys.readouterr().out
    assert out.startswith("build: ok ds-") and "forecast kept, " in out and "ARIMA_PLUS fallback for 1" in out
    assert (root / "parts" / "forecast_points.csv.gz").read_bytes() == points  # reused, not recomputed

    info = M.release_check(root)
    m = manifest_of(root)
    assert info["dataset_version"] == m["dataset_version"] != manifest_of(built)["dataset_version"]
    assert info["providers"] == m["forecast"]["providers"] == {
        "ai_forecast": N_SERIES * POINTS_PER_SERIES - SPOILED_POINTS, "arima_plus": SPOILED_POINTS}
    assert m["forecast"]["fallbacks"] == 1 and m["forecast"]["stand_in"] is False and m["forecast"]["note"] == ""
    d1 = next(r for r in m["forecast"]["runs"] if r["job"] == "d1")
    assert d1["providers"] == {"ai_forecast": 9, "arima_plus": 1} and d1["fallbacks"] == 1
    assert m["input_version"] == manifest_of(built)["input_version"]  # the stages repeat; the forecast is outside it
    assert "test fixture" in info["note"]


@pytest.mark.parametrize("name", ["forecast_points.csv.gz", "forecast_runs.csv.gz", "forecast_input.csv.gz",
                                  "forecast_input.json"])
def test_keep_refuses_a_missing_forecast_part_before_any_stage(copy_of, monkeypatch, tmp_path, capsys, name):
    root = copy_of()
    (root / "parts" / name).unlink()
    monkeypatch.setattr(B, "stock_stage", lambda *a, **k: pytest.fail("keep refuses before the stock stage"))
    no_provider(monkeypatch)
    assert keep(root, tmp_path / "reports") == 2
    assert f"--forecast keep: no {name}" in capsys.readouterr().err


def test_keep_refuses_an_input_part_whose_bytes_are_not_its_meta(copy_of, monkeypatch, tmp_path, capsys):
    root = copy_of()
    path = root / "parts" / "forecast_input.csv.gz"
    data = bytearray(path.read_bytes())
    data[-9] ^= 0x01  # inside the gzip trailer's CRC: the bytes differ, so the sha256 does
    path.write_bytes(bytes(data))
    no_provider(monkeypatch)
    assert keep(root, tmp_path / "reports") == 2
    assert "sha256" in capsys.readouterr().err


def test_keep_refuses_a_run_built_on_other_inputs(copy_of, monkeypatch, tmp_path, capsys):
    root = copy_of()

    def other(header, rows):
        i = header.index("meta")
        meta = json.loads(rows[1][i])
        meta["reference_history_id"] = "fb-000000000000"
        rows[1][i] = C.canonical(meta)
        return rows
    edit_part(root, "forecast_runs", other)
    no_provider(monkeypatch)
    assert keep(root, tmp_path / "reports") == 2
    err = capsys.readouterr().err
    assert "built on other inputs" in err and "fb-000000000000" in err


def test_keep_on_the_stand_in_rebuilds_the_same_set_and_the_release_check_still_refuses_it(built, copy_of,
                                                                                           monkeypatch, tmp_path):
    root = copy_of()
    no_provider(monkeypatch)
    assert keep(root, tmp_path / "reports") == 0
    assert files_of(root) == files_of(built)  # keep changes nothing but what the parts say
    with pytest.raises(M.ManifestError, match="local-seasonal-naive"):
        M.release_check(root)
