"""Public dataset rows from a checked file (05.2-01; DATA-08, DATA-10; D-02, D-06, D-13, D-14, D-17).

The rows come from tests/data/fixtures/frn_sample_synthetic.csv, a synthetic test fixture in the FreshRetailNet-50K
schema; not FreshRetailNet rows. Every expected count is derived by hand from the generator's shape (5 stores x 2
products x 49 days = 490 rows; 42 train days, 7 eval days) and its tables STOCKOUTS, DISCOUNTS, UNKNOWN_PRICE,
ZERO_IN_STOCK and ONE_UNIT, as the comments show. The calendar facts are the reference's own: +840 days = 120 weeks,
2024-06-26 (Wed) -> day 0 = 2026-10-14 (Wed).
"""
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
from datetime import date, timedelta

import pytest

from data import public as U
from data import public_config as P
from data.config import ConfigError

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
CSV, SIDECAR = FIX / "frn_sample_synthetic.csv", FIX / "frn_sample_synthetic.json"
ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def ref():
    return P.load()


@pytest.fixture(scope="module")
def fix():
    return P.load_for(SIDECAR)


@pytest.fixture(scope="module")
def fixture_rows(fix):
    return list(U.day_rows(U.read_csv(CSV, fix["dataset"]["columns"]), fix))


def raw(**changes):
    """One raw row as read_csv yields it (every value a string), with `changes` applied."""
    row = {"city_id": "1", "store_id": "11", "management_group_id": "1", "first_category_id": "10",
           "second_category_id": "100", "third_category_id": "1000", "product_id": "101", "dt": "2024-06-26",
           "sale_amount": "1.5", "stock_hour6_22_cnt": "0", "discount": "1.0", "holiday_flag": "0",
           "activity_flag": "0", "precpt": "0.0", "avg_temperature": "28.5", "avg_humidity": "70.0",
           "avg_wind_level": "2.0"}
    row.update(changes)
    return row


def one(cfg, **changes):
    return next(U.day_rows([raw(**changes)], cfg))


def expected(cfg):
    files = cfg["dataset"]["files"]
    return files["train"]["sha256"], files["train"]["bytes"]


# --- DATA-08, D-06: a file is trusted only after its checksum -------------------------------------------------------

def test_a_changed_byte_is_refused_before_any_row_is_read(tmp_path, monkeypatch, capsys):
    copy = tmp_path / CSV.name
    data = bytearray(CSV.read_bytes())
    at = data.index(b"2024-06-01")  # one digit of one date: the size stays the same, the hash does not
    data[at + 3] = ord("5")
    copy.write_bytes(bytes(data))
    sha, size = expected(P.load_for(SIDECAR))
    with pytest.raises(U.PublicDataError, match=r"frn_sample_synthetic\.csv: .*sha256"):
        U.verify(copy, sha, size)

    def no_reading(*args, **kwargs):
        raise AssertionError("a row was read from a file that failed its check")

    monkeypatch.setattr(U, "read_csv", no_reading)
    assert U.main(["profile", "--csv", str(copy), "--fixture", str(SIDECAR)]) == 2
    assert "frn_sample_synthetic.csv" in capsys.readouterr().err


def test_a_wrong_byte_size_is_refused_naming_the_file(tmp_path):
    copy = tmp_path / CSV.name
    copy.write_bytes(CSV.read_bytes() + b"\n")
    sha, size = expected(P.load_for(SIDECAR))
    with pytest.raises(U.PublicDataError, match=r"frn_sample_synthetic\.csv: .*bytes"):
        U.verify(copy, sha, size)


def test_the_untouched_fixture_passes_its_check(fix):
    sha, size = expected(fix)
    assert U.sha256_of(CSV) == sha
    U.verify(CSV, sha, size)


def test_a_file_the_reference_does_not_name_is_refused(tmp_path, capsys):
    other = tmp_path / "other.csv"
    shutil.copyfile(CSV, other)
    assert U.main(["profile", "--csv", str(other), "--fixture", str(SIDECAR)]) == 2
    assert "other.csv" in capsys.readouterr().err


def test_the_reference_holds_both_files_checksums_sizes_rows_and_dates(ref):
    train, ev = ref["dataset"]["files"]["train"], ref["dataset"]["files"]["eval"]
    assert (train["name"], train["bytes"], train["rows"], train["first_dt"], train["last_dt"]) == (
        "train.parquet", 106436287, 4500000, "2024-03-28", "2024-06-25")
    assert (ev["name"], ev["bytes"], ev["rows"], ev["first_dt"], ev["last_dt"]) == (
        "eval.parquet", 8440124, 350000, "2024-06-26", "2024-07-02")
    assert train["sha256"].startswith("6706832d") and ev["sha256"].startswith("1b118840")
    assert len(ref["dataset"]["columns"]) == 17  # the card's 19 minus hours_sale and hours_stock_status


def test_a_missing_column_is_refused_naming_it(tmp_path, fix):
    lines = CSV.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    drop = header.index("discount")
    path = tmp_path / "short.csv"
    path.write_text("\n".join(",".join(c for i, c in enumerate(line.split(",")) if i != drop) for line in lines),
                    encoding="utf-8")
    with pytest.raises(U.PublicDataError, match="discount"):
        list(U.read_csv(path, fix["dataset"]["columns"]))


# --- DATA-10, D-13: the weekday-preserving shift ----------------------------------------------------------------------

def test_the_train_end_is_day_minus_1_and_the_eval_start_day_0(ref):
    assert one(ref, dt="2024-06-25")["day"] == -1
    assert one(ref, dt="2024-06-26")["day"] == 0


def test_the_first_train_date_is_day_minus_90_and_the_last_eval_date_day_6(ref):
    # 2024-06-26 - 2024-03-28 = 3 + 31 + 30 + 26 = 90 days; 2024-07-02 is 6 days after 2024-06-26
    assert one(ref, dt="2024-03-28")["day"] == -90
    assert one(ref, dt="2024-07-02")["day"] == 6


def test_every_shifted_date_keeps_its_weekday(fix, fixture_rows):
    shift, day0 = timedelta(days=fix["calendar"]["shift_days"]), date.fromisoformat(fix["calendar"]["day0"])
    assert len(fixture_rows) == 490
    for row in fixture_rows:
        source = date.fromisoformat(row["dt"])
        assert source + shift == day0 + timedelta(days=row["day"])
        assert (source + shift).weekday() == source.weekday()
    assert {r["day"] for r in fixture_rows} == set(range(-42, 7))  # 42 train days, then days 0..6


def test_a_dt_outside_the_dataset_range_is_refused_naming_the_row(ref, fix):
    for cfg, dt in ((ref, "2024-03-27"), (ref, "2024-07-03"), (fix, "2024-05-14")):
        with pytest.raises(U.PublicDataError, match=rf"store 11, product 101, dt {dt}"):
            one(cfg, dt=dt)


def test_data_as_of_is_day0_plus_demo_open_day(ref):
    # day0 Wed 14 Oct 2026 + 3 days = Sat 17 Oct 2026 (D-13)
    as_of = date.fromisoformat(ref["calendar"]["data_as_of"])
    assert as_of == date.fromisoformat(ref["calendar"]["day0"]) + timedelta(days=ref["calendar"]["demo_open_day"])
    assert as_of.weekday() == 5


def write_reference(tmp_path, change):
    tree = json.loads((ROOT / "reference" / "public.json").read_text(encoding="utf-8"))
    change(tree)
    (tmp_path / "public.json").write_text(json.dumps(tree), encoding="utf-8")
    return tmp_path


def test_a_shift_that_breaks_a_weekday_is_refused_naming_calendar_shift_days(tmp_path):
    ref_in = write_reference(tmp_path, lambda t: t["calendar"]["shift_days"].update(value=841))
    with pytest.raises(ConfigError, match=r"calendar\.shift_days"):
        P.load(ref_in)


def test_a_data_as_of_that_is_not_day0_plus_demo_open_day_is_refused(tmp_path):
    ref_in = write_reference(tmp_path, lambda t: t["calendar"]["data_as_of"].update(value="2026-10-16"))
    with pytest.raises(ConfigError, match=r"calendar\.data_as_of"):
        P.load(ref_in)


def test_a_fixture_override_may_not_add_a_path_the_reference_lacks(tmp_path):
    tree = json.loads(SIDECAR.read_text(encoding="utf-8"))
    tree["calendar"]["shift_dayz"] = tree["calendar"]["first_day"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(tree), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"calendar\.shift_dayz"):
        P.load_for(path)


# --- D-14, D-17, D-02: flags and codes ------------------------------------------------------------------------------

def test_stockout_is_any_out_of_stock_hour(ref):
    # reference threshold 0: a day with any hour out of stock 06-22 is a stockout
    assert [one(ref, stock_hour6_22_cnt=str(n))["stockout"] for n in (0, 1, 16)] == [False, True, True]
    assert one(ref, stock_hour6_22_cnt="7")["oos_hours"] == 7


def test_discount_zero_and_above_one_are_unknown_price(ref):
    flags = [one(ref, discount=d)["unknown_price"] for d in ("0.0", "1.05", "1.0", "0.8")]
    assert flags == [True, True, False, False]
    assert one(ref, discount="0.8")["discount"] == 0.8


def test_codes_are_strings_and_no_row_carries_a_name(fixture_rows):
    for row in fixture_rows:
        for key in ("city", "store", "mgmt", "cat1", "cat2", "cat3", "product"):
            assert isinstance(row[key], str) and row[key].isdigit(), (key, row[key])
        assert not [key for key in row if "name" in key]
    assert {r["store"] for r in fixture_rows} == {"11", "12", "13", "21", "22"}


def test_a_negative_sale_or_an_out_of_range_oos_count_is_refused_naming_the_row(ref):
    for changes in ({"sale_amount": "-0.1"}, {"stock_hour6_22_cnt": "17"}, {"stock_hour6_22_cnt": "-1"},
                    {"avg_temperature": "warm"}, {"sale_amount": "nan"}, {"store_id": "S11"}):
        with pytest.raises(U.PublicDataError, match=r"product 101, dt 2024-06-26"):
            one(ref, **changes)


def test_the_split_row_counts_match_the_sidecar(fix, fixture_rows):
    # 5 stores x 2 products x 42 train days = 420; x 7 eval days = 70
    assert sum(r["day"] < 0 for r in fixture_rows) == fix["dataset"]["files"]["train"]["rows"] == 420
    assert sum(r["day"] >= 0 for r in fixture_rows) == fix["dataset"]["files"]["eval"]["rows"] == 70


# --- profile ----------------------------------------------------------------------------------------------------------

def test_the_profile_equals_the_hand_counts(capsys):
    assert U.main(["profile", "--csv", str(CSV), "--fixture", str(SIDECAR)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"note", "rows", "series", "stores", "cities", "products", "splits", "categories", "units_factor",
                        "unknown_price", "stockout", "zero_sales", "discount", "spread", "codes"}
    # 2 cities, 5 stores, 2 products; 5 stores x 2 products = 10 series
    assert (out["rows"], out["series"], out["stores"], out["cities"], out["products"]) == (490, 10, 5, 2, 2)
    # the note names the dataset, licence and creator the config holds: here the fixture override's own identity
    assert out["note"].startswith("Profile of the public frn_sample_synthetic files (CC BY 4.0, ShelfShift test fixture")
    # one management group, cat1 and cat2 hold all 10 series; cat3 1000 holds product 101 in 5 stores, 1001 product 102
    assert out["categories"] == {
        "mgmt": {"codes": 1, "series_per_code": {"min": 10, "median": 10, "max": 10}, "series_on_two_codes": 0},
        "cat1": {"codes": 1, "series_per_code": {"min": 10, "median": 10, "max": 10}, "series_on_two_codes": 0},
        "cat2": {"codes": 1, "series_per_code": {"min": 10, "median": 10, "max": 10}, "series_on_two_codes": 0},
        "cat3": {"codes": 2, "series_per_code": {"min": 5, "median": 5, "max": 5}, "series_on_two_codes": 0}}
    # city 1 has stores 11, 12, 13 and city 2 has 21, 22: min 2, median (2 + 3) / 2, p90 (nearest rank 2 of 2) 3, max 3;
    # every store carries both products; city 1 holds 3 x 2 = 6 series, city 2 holds 2 x 2 = 4
    assert out["spread"] == {
        "stores_per_city": {"min": 2, "median": 2.5, "p90": 3, "max": 3, "by_city": {"1": 3, "2": 2}},
        "series_per_store": {"min": 2, "median": 2, "p90": 2, "max": 2, "by_city": {"1": 6, "2": 4}}}
    assert out["splits"] == {
        "train": {"rows": 420, "first_dt": "2024-05-15", "last_dt": "2024-06-25", "first_day": -42, "last_day": -1},
        "eval": {"rows": 70, "first_dt": "2024-06-26", "last_dt": "2024-07-02", "first_day": 0, "last_day": 6}}
    # STOCKOUTS: hours 3, 8, 12, 16, 1, 9 -> 6 stockout days, 4 of them 8 hours or more (8, 12, 16, 9). Each store has
    # 2 products x 49 days = 98 rows; store 12 has 2 stockout days, stores 11, 13, 21 and 22 one each
    assert out["stockout"] == {"days": 6, "share": 6 / 490, "long_days": 4, "long_share": 4 / 490,
                               "per_store_share": {"min": 1 / 98, "median": 1 / 98, "max": 2 / 98}}
    # zero sales: the ZERO_IN_STOCK day and the 16-hour stockout day; in stock: 1 of 490 - 6 = 484 days
    assert out["zero_sales"] == {"rows": 2, "share": 2 / 490, "in_stock_rows": 1, "in_stock_share": 1 / 484}
    # DISCOUNTS 0.8, 0.9, 0.7, 0.85 are 20, 10, 30 and 15 percent off: (0, 10] one, (10, 20] two, (20, 30] one
    assert out["discount"] == {"rows": 4, "share": 4 / 490,
                               "depth": {"0-10": 1, "10-20": 2, "20-30": 1, "over_30": 0}}
    # UNKNOWN_PRICE: one 0.0 and one 1.05
    assert out["unknown_price"] == {"rows": 2, "at_or_below": 1, "above": 1}
    assert out["codes"] == {
        "max_id": {"city": 2, "store": 22, "product": 102, "mgmt": 1, "cat1": 10, "cat2": 100, "cat3": 1001},
        "stores_by_city": {"1": ["11", "12", "13"], "2": ["21", "22"]}}
    # sale_amount = units x 0.1 and ONE_UNIT sells 1 unit, so the smallest positive value is 0.1 and every positive
    # value sits on its lattice; the 12-hour stockout day sells 2 units (0.2), the next lattice step. Positive rows:
    # 490 minus the 2 zero-sales rows
    units = out["units_factor"]
    assert units["smallest_positive"] == 0.1 and units["lattice_share"] == 1.0 and units["positive_rows"] == 488
    assert units["first_values"][:2] == [0.1, 0.2] and units["first_values"] == sorted(units["first_values"])
    assert len(units["first_values"]) <= 50 and units["distinct_positive_values"] >= len(units["first_values"])


def test_series_per_category_code_counts_distinct_series_under_each_code(ref):
    # cat3 1000 holds series (11, 101), (11, 102) and (12, 101), the second row of (11, 101) adding nothing;
    # cat3 1001 holds (12, 103): 2 codes, series per code 1 and 3, median (1 + 3) / 2 = 2
    rows = [one(ref, store_id=s, product_id=p, third_category_id=c, dt=dt) for s, p, c, dt in (
        ("11", "101", "1000", "2024-06-26"), ("11", "101", "1000", "2024-06-27"), ("11", "102", "1000", "2024-06-26"),
        ("12", "101", "1000", "2024-06-26"), ("12", "103", "1001", "2024-06-26"))]
    assert U.profile(rows, ref)["categories"]["cat3"] == {
        "codes": 2, "series_per_code": {"min": 1, "median": 2, "max": 3}, "series_on_two_codes": 0}
    # a series that shows under two codes is counted under both and reported
    rows.append(one(ref, store_id="12", product_id="103", third_category_id="1000", dt="2024-06-27"))
    assert U.profile(rows, ref)["categories"]["cat3"]["series_on_two_codes"] == 1


def test_a_discount_depth_on_a_bucket_edge_falls_in_the_lower_bucket(ref):
    # 1 - 0.7 is 0.30000000000000004 in floating point; 30 percent off is still the 20-30 bucket
    rows = [one(ref, discount=d, dt=f"2024-06-{26 + i}") for i, d in enumerate(("0.7", "0.69", "0.95"))]
    assert U.profile(rows, ref)["discount"]["depth"] == {"0-10": 1, "10-20": 0, "20-30": 1, "over_30": 1}


def test_the_profile_note_credits_the_dataset_from_the_reference(ref):
    assert U.profile([], ref)["note"] == ("Profile of the public FreshRetailNet-50K files (CC BY 4.0, Dingdong-Inc); "
                                          "aggregates only; figures describe the public data, not any retailer.")


class OnceOnly:
    """Rows that can be iterated once: profile must stream, never re-read or count up front."""

    def __init__(self, rows):
        self.rows, self.used = rows, False

    def __iter__(self):
        assert not self.used, "profile iterated the rows twice"
        self.used = True
        return iter(self.rows)


def test_the_profile_streams_its_rows_once(ref, fixture_rows, fix):
    rows = OnceOnly(fixture_rows)
    assert U.profile(rows, fix)["rows"] == 490 and rows.used
    assert U.profile((r for r in fixture_rows), fix)["series"] == 10


def test_a_value_off_the_lattice_lowers_the_lattice_share(ref):
    # positives 0.1, 0.2, 0.25: 0.25 / 0.1 = 2.5 is no integer multiple, so 2 of 3 values are on the lattice
    rows = [one(ref, sale_amount=v, dt=f"2024-06-{26 + i}") for i, v in enumerate(("0.1", "0.2", "0.25", "0.0"))]
    assert U.profile(rows, ref)["units_factor"]["lattice_share"] == 2 / 3


# --- the fixture and the module ---------------------------------------------------------------------------------------

def test_the_fixture_is_its_generator_output(tmp_path):
    spec = importlib.util.spec_from_file_location("make_frn_sample", FIX / "make_frn_sample.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    csv_path, json_path = gen.write(tmp_path)
    assert csv_path.read_bytes() == CSV.read_bytes()
    assert json_path.read_bytes() == SIDECAR.read_bytes()


def test_the_fixture_is_labelled_synthetic():
    label = "synthetic test fixture in the FreshRetailNet-50K schema; not FreshRetailNet rows"
    assert json.loads(SIDECAR.read_text(encoding="utf-8"))["label"] == label
    assert label in (FIX / "make_frn_sample.py").read_text(encoding="utf-8")


def test_pyarrow_is_never_imported_at_module_level():
    source = (ROOT / "data" / "public.py").read_text(encoding="utf-8")
    assert not [line for line in source.splitlines() if line.startswith(("import pyarrow", "from pyarrow"))]


# --- 05.2-03: the parquet reader and the isolated entry point (DATA-08, D-06, D-07) ---------------------------------

def test_the_parquet_reader_imports_pyarrow_only_when_called():
    # a fresh interpreter: importing data.public, and even building the reader's generator, leaves pyarrow unloaded
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import data.public as U; "
            "U.read_parquet('never-read.parquet', ['dt']); print('pyarrow' in sys.modules)")
    done = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "False"


def split_reader(fix, drop=0, stray=False):
    """A stand-in for read_parquet on the fixture: the first call yields the train rows, the second the eval rows (the
    sidecar names one CSV for both splits). `drop` removes that many train rows; `stray` moves the first eval row into
    the train file."""
    rows = list(U.read_csv(CSV, fix["dataset"]["columns"]))
    last_train = fix["dataset"]["files"]["train"]["last_dt"]
    train, ev = [r for r in rows if r["dt"] <= last_train], [r for r in rows if r["dt"] > last_train]
    if stray:
        train = train + ev[:1]
    calls = iter((train[drop:], ev))
    seen = []

    def reader(path, columns, batch_rows=None):
        seen.append(pathlib.Path(path).name)
        return iter(next(calls))

    return reader, seen


def test_read_files_yields_train_then_eval_after_both_checks(fix, monkeypatch):
    reader, seen = split_reader(fix)
    monkeypatch.setattr(U, "read_parquet", reader)
    stats = {}
    rows = list(U.read_files(FIX, fix, stats))
    # 420 train rows (5 stores x 2 products x 42 days), then 70 eval rows (x 7 days)
    assert len(rows) == 490 and rows[0]["dt"] == "2024-05-15" and rows[-1]["dt"] == "2024-07-02"
    assert seen == [CSV.name, CSV.name]
    assert stats == {"train": {"name": CSV.name, "rows": 420, "first_dt": "2024-05-15", "last_dt": "2024-06-25"},
                     "eval": {"name": CSV.name, "rows": 70, "first_dt": "2024-06-26", "last_dt": "2024-07-02"}}


def test_read_files_refuses_a_wrong_row_count(fix, monkeypatch):
    reader, _ = split_reader(fix, drop=1)
    monkeypatch.setattr(U, "read_parquet", reader)
    with pytest.raises(U.PublicDataError, match=r"frn_sample_synthetic\.csv: 419 rows, expected 420"):
        list(U.read_files(FIX, fix))


def test_read_files_refuses_a_dt_outside_its_split(fix, monkeypatch):
    reader, _ = split_reader(fix, stray=True)
    monkeypatch.setattr(U, "read_parquet", reader)
    with pytest.raises(U.PublicDataError, match=r"dt 2024-06-26 .*train 2024-05-15\.\.2024-06-25"):
        list(U.read_files(FIX, fix))


def test_read_files_checks_both_files_before_reading_a_row(fix, tmp_path, monkeypatch):
    data = bytearray(CSV.read_bytes())
    data[data.index(b"2024-06-01") + 3] = ord("5")  # same size, different hash
    (tmp_path / CSV.name).write_bytes(bytes(data))

    def no_reading(*args, **kwargs):
        raise AssertionError("a row was read before both files passed their check")

    monkeypatch.setattr(U, "read_parquet", no_reading)
    with pytest.raises(U.PublicDataError, match=r"frn_sample_synthetic\.csv: .*sha256"):
        next(U.read_files(tmp_path, fix))


def test_verify_prints_one_ok_line_per_file_and_refuses_with_exit_2(fix, monkeypatch, capsys):
    reader, _ = split_reader(fix)
    monkeypatch.setattr(U, "read_parquet", reader)
    assert U.main(["verify", "--raw", str(FIX), "--fixture", str(SIDECAR)]) == 0
    assert capsys.readouterr().out.splitlines() == [f"ok {CSV.name} 420 rows 2024-05-15..2024-06-25",
                                                    f"ok {CSV.name} 70 rows 2024-06-26..2024-07-02"]
    reader, _ = split_reader(fix, drop=2)
    monkeypatch.setattr(U, "read_parquet", reader)
    assert U.main(["verify", "--raw", str(FIX), "--fixture", str(SIDECAR)]) == 2
    assert "418 rows, expected 420" in capsys.readouterr().err


@pytest.fixture()
def frn():
    spec = importlib.util.spec_from_file_location("frn_entry", ROOT / "tools" / "frn.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_entry_point_names_every_pipeline_command_of_the_phase(frn):
    assert set(frn.COMMANDS) == {"verify", "profile", "measure", "stock", "network", "build", "check", "forecast",
                                 "backtest", "assumptions"}
    assert frn.COMMANDS["verify"] == frn.COMMANDS["profile"] == "data.public:main"
    assert str(ROOT) in sys.path  # the entry point puts the repo root on sys.path itself (it runs under -I)


def test_the_entry_point_dispatches_and_a_missing_module_is_not_available_yet(frn, monkeypatch, capsys):
    monkeypatch.setitem(frn.COMMANDS, "later", "data.no_such_module_yet:main")
    assert frn.main(["later", "--anything"]) == 2
    assert capsys.readouterr().err.strip() == "later: not available yet"
    assert frn.main(["nonsense"]) == 2
    assert frn.main(["profile", "--csv", str(CSV), "--fixture", str(SIDECAR)]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == 490
