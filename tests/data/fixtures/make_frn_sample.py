"""Writes frn_sample_synthetic.csv and its sidecar frn_sample_synthetic.json: a synthetic test fixture in the
FreshRetailNet-50K schema; not FreshRetailNet rows. No public row is in git (05.2 D-06); this file is test data designed
to exercise the paths the 05.2 plans test, and every special row is listed in a table below so the tests can count it
by hand.

    python tests/data/fixtures/make_frn_sample.py [OUT_DIR]     (default: this folder)

Shape: 2 cities (city "1" with stores "11", "12", "13"; city "2" with stores "21", "22"), 2 products ("101" in
categories 1/10/100/1000, "102" in 1/10/100/1001), dt 2024-05-15 .. 2024-07-02 (42 train days, then the 7 eval days),
the 17 projected columns, rows ordered by store, product, dt: 5 x 2 x 49 = 490 rows (420 train, 70 eval).

- sale_amount is units x STEP (0.1), so the positive values sit on one lattice; one row sells 1 unit, so the smallest
  positive value is STEP.
- STOCKOUTS: 6 days with out-of-stock hours (4 of them 8 hours or more); sales shrink with the hours out, and the
  16-hour day sells nothing.
- DISCOUNTS: 4 discounted days. UNKNOWN_PRICE: one row with discount 0.0 and one with 1.05. Every other row is 1.0.
- ZERO_IN_STOCK: one in-stock day with no sales.
- holiday_flag is 1 on Saturdays and Sundays; activity_flag is 1 on the discounted days.
- Store "13" is the store the fixture's geography leaves unmapped (05.2-10).
- The open day's reviewer cases (05.2-16: the catalogue evaluates the lots the day-3 screen flags and tags one case
  per outcome kind). With one weekday shape for every series, deliveries sized like the forecast and the markdown
  schedule's lift, the day-3 screen flagged no lot at all, so the series below are shaped to give the judge's kinds.
  This is test data only: the release set is never shaped, and no rule, seed value or engine knob changes for it.
  - WEEKEND: a series whose weekend sells far below its Thursday and Friday (factors for Saturday and Sunday), so the
    lot delivered on Thursday for Thursday and Friday is still on hand at the weekend and Keep wastes some of it.
  - EVAL_FACTOR: the eval week (the hidden demand of days 0..6) against history. "12"/"101" sells 1.6x, so its
    eval week has top-ups (the stock tests' eval-week shortfall series).
  - "12"/"101", the designed series (physical 0 from day 0, record unchanged): BASE 24 and WEEKEND (0.5, 0.1), so its
    recorded lot wastes under Keep while Saturday's forecast stays above the phantom rule's minimum; "11" sells
    "101" at 1.8x in the eval week, runs short and can take a transfer, and the phantom fact at "12" changes that
    pick: verify before you move, on the designed lot.
  - "21"/"101": WEEKEND (0.1, 0.1) and an eval week at 0.3x leave a surplus; "22" sells "101" at 3.0x and runs
    short: a transfer on the cold-chain run "21" -> "22".
  - "13"/"101" and "13"/"102" (BASE 10): WEEKEND (0.2, 0.2) and an eval week at 0.2x: Keep wastes at the unmapped
    store, which has no run (transfers unavailable). Two such lots, so Keep and transfers-unavailable each find a
    case under the catalogue's one-tag-per-case rule.

The sidecar also holds the fixture's own settings for `public_config.load_for` (test data only; the release reference
sets none of them differently, 05.2-10):
- `units.factor` = 1 / STEP = 10: the fixture's sale_amount is units x STEP, so the factor gives whole units back.
- `geography`: store "13" unmapped (the D-31 path: excluded from transfers, counted), and a service limit of
  FIXTURE_MAX_TRAVEL_MIN = 90 min. With the reference positions the fixture's travel times are 11-12 63 min and 21-22
  69 min (11-13 49, 12-13 43), all over the reference 60 min once 13 is out, so without its own limit the fixture
  network would have no run at all.
- `designed.verify_example`: store "12", product "101", physical 0 from day 0 with the record unchanged, labelled
  "Designed example (test fixture)", so the API tests can exercise the count path (D-18).

Every draw comes from `random.Random` keyed by a string ("frn-sample:<store>:<product>:<dt>" for sales, and
"frn-sample:weather:<store>:<dt>" so both products of a store-day share their weather), so the bytes are the same on
every run and under every PYTHONHASHSEED.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import pathlib
import random
import sys
from datetime import date, timedelta

HERE = pathlib.Path(__file__).resolve().parent
CSV_NAME, JSON_NAME = "frn_sample_synthetic.csv", "frn_sample_synthetic.json"
LABEL = "synthetic test fixture in the FreshRetailNet-50K schema; not FreshRetailNet rows"
SOURCE = "tests/data/fixtures/make_frn_sample.py (" + LABEL + ")"
COLUMNS = ("city_id", "store_id", "management_group_id", "first_category_id", "second_category_id",
           "third_category_id", "product_id", "dt", "sale_amount", "stock_hour6_22_cnt", "discount", "holiday_flag",
           "activity_flag", "precpt", "avg_temperature", "avg_humidity", "avg_wind_level")
STORES = (("1", "11"), ("1", "12"), ("1", "13"), ("2", "21"), ("2", "22"))  # (city, store)
PRODUCTS = {"101": ("1", "10", "100", "1000"), "102": ("1", "10", "100", "1001")}  # mgmt, cat1, cat2, cat3
FIRST, EVAL_FIRST, LAST = date(2024, 5, 15), date(2024, 6, 26), date(2024, 7, 2)
FIRST_DAY = -42  # FIRST is 42 days before EVAL_FIRST, which the shifted calendar maps to day 0
STEP = 0.1
HOURS = 16
BASE = {("11", "101"): 20, ("12", "101"): 24, ("13", "101"): 10, ("21", "101"): 14, ("22", "101"): 12,
        ("11", "102"): 8, ("12", "102"): 6, ("13", "102"): 10, ("21", "102"): 7, ("22", "102"): 9}
WEEKDAY = (0.9, 0.9, 0.95, 1.0, 1.1, 1.3, 1.2)  # Mon..Sun
# the open day's reviewer cases (05.2-16; see the module doc): the eval week against history, and weak weekends
EVAL_FACTOR = {("11", "101"): 1.8, ("12", "101"): 1.6, ("13", "101"): 0.2, ("13", "102"): 0.2, ("21", "101"): 0.3, ("22", "101"): 3.0}
WEEKEND = {("12", "101"): (0.5, 0.1), ("13", "101"): (0.2, 0.2), ("13", "102"): (0.2, 0.2),
           ("21", "101"): (0.1, 0.1)}  # (Saturday, Sunday) factors
STOCKOUTS = {("11", "101", "2024-05-20"): 3, ("12", "101", "2024-05-28"): 8, ("13", "102", "2024-06-03"): 12,
             ("21", "101", "2024-06-10"): 16, ("22", "102", "2024-06-15"): 1, ("12", "102", "2024-06-29"): 9}
DISCOUNTS = {("11", "102", "2024-05-24"): 0.8, ("21", "102", "2024-06-07"): 0.9, ("22", "101", "2024-06-14"): 0.7,
             ("13", "101", "2024-06-21"): 0.85}
UNKNOWN_PRICE = {("12", "101", "2024-06-05"): 0.0, ("22", "102", "2024-06-12"): 1.05}
ZERO_IN_STOCK = {("13", "102", "2024-05-30")}
ONE_UNIT = {("13", "102", "2024-06-18")}
UNMAPPED = ["13"]
FIXTURE_MAX_TRAVEL_MIN = 90
DESIGNED = {"store": "12", "product": "101",
            "rule": "test data: one series with physical 0 from day 0, its record unchanged",
            "label": "Designed example (test fixture)"}


def _units(store: str, product: str, day: date) -> int:
    key = (store, product, day.isoformat())
    if key in ZERO_IN_STOCK:
        return 0
    if key in ONE_UNIT:
        return 1
    rng = random.Random(f"frn-sample:{store}:{product}:{day.isoformat()}")
    level = BASE[(store, product)] * WEEKDAY[day.weekday()] * rng.uniform(0.8, 1.2)
    if day.weekday() >= 5 and (store, product) in WEEKEND:
        level *= WEEKEND[(store, product)][day.weekday() - 5]
    if day >= EVAL_FIRST:
        level *= EVAL_FACTOR.get((store, product), 1.0)
    if key in DISCOUNTS:
        level *= 1.2
    hours_out = STOCKOUTS.get(key, 0)
    if hours_out >= HOURS:
        return 0
    return max(2, round(level * (HOURS - hours_out) / HOURS))


def rows() -> list[list[str]]:
    out = []
    for city, store in STORES:
        for product, cats in PRODUCTS.items():
            day = FIRST
            while day <= LAST:
                dt = day.isoformat()
                key = (store, product, dt)
                weather = random.Random(f"frn-sample:weather:{store}:{dt}")
                discount = DISCOUNTS.get(key, UNKNOWN_PRICE.get(key, 1.0))
                out.append([city, store, *cats, product, dt, repr(round(_units(store, product, day) * STEP, 6)),
                            str(STOCKOUTS.get(key, 0)), repr(discount), "1" if day.weekday() >= 5 else "0",
                            "1" if key in DISCOUNTS else "0",
                            repr(round(max(0.0, weather.uniform(-5.0, 15.0)), 2)),
                            repr(round(weather.uniform(22.0, 33.0), 2)), repr(round(weather.uniform(55.0, 95.0), 2)),
                            repr(round(weather.uniform(1.0, 4.0), 2))])
                day += timedelta(days=1)
    return out


def _leaf(value, note: str | None = None, type_: str = "Data") -> dict:
    leaf = {"value": value, "type": type_, "source": SOURCE}
    if note:
        leaf["note"] = note
    return leaf


def write(out_dir: pathlib.Path | str = HERE) -> tuple[pathlib.Path, pathlib.Path]:
    """Write the CSV and its sidecar into `out_dir`; returns their paths."""
    out_dir = pathlib.Path(out_dir)
    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\n")
    writer.writerow(COLUMNS)
    body = rows()
    writer.writerows(body)
    data = text.getvalue().encode("utf-8")
    csv_path = out_dir / CSV_NAME
    csv_path.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()

    def split(first: date, last: date) -> dict:
        n = sum(1 for r in body if first.isoformat() <= r[7] <= last.isoformat())
        return {"name": _leaf(CSV_NAME), "sha256": _leaf(sha), "bytes": _leaf(len(data)), "rows": _leaf(n),
                "first_dt": _leaf(first.isoformat()), "last_dt": _leaf(last.isoformat())}

    sidecar = {
        "label": LABEL,
        "dataset": {
            "name": _leaf("frn_sample_synthetic", LABEL),
            "creator": _leaf("ShelfShift test fixture (synthetic)"),
            "files": {"train": split(FIRST, EVAL_FIRST - timedelta(days=1)), "eval": split(EVAL_FIRST, LAST)},
        },
        "calendar": {"first_day": _leaf(FIRST_DAY, "the fixture's first train date")},
        "units": {"factor": _leaf(round(1 / STEP), "1 / STEP: the fixture's sale_amount is units x STEP", "Assumption")},
        "geography": {
            "coverage": {"unmapped": _leaf(UNMAPPED, "the fixture's unmapped store (the D-31 path)", "Assumption")},
            "service": {"max_travel_min": _leaf(FIXTURE_MAX_TRAVEL_MIN, "the fixture's own service limit, so its "
                                                "mapped pairs have a run", "Policy")},
        },
        "designed": {"verify_example": _leaf(DESIGNED, "the fixture's designed example (test data only)", "Policy")},
    }
    json_path = out_dir / JSON_NAME
    json_path.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return csv_path, json_path


if __name__ == "__main__":
    for path in write(pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else HERE):
        print(f"wrote {path}")
