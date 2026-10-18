"""Public dataset rows from checked files: .venv-data/bin/python -I tools/frn.py {verify,profile} (spec §5 Public
dataset (rev 6.6); DATA-08, DATA-09, DATA-10).

A FreshRetailNet-50K file is untrusted bytes from the internet (05.2-CONTEXT D-06). It is read only after its byte size
and sha256 equal the values in reference/public.json `dataset.files`; any other file is refused with PublicDataError
naming the file, before a row is read. The files are read, never executed and never unpickled, and the pipeline runs
with `python -I`.

Each raw row becomes a typed day row on the shifted calendar (D-13): the source date plus `calendar.shift_days` (840
days, 120 weeks, so the weekday is kept) minus `calendar.day0` is the day index, and a date outside
[`calendar.first_day`, `calendar.last_day`] is refused naming the row. City, store, product and the four category
levels stay codes, kept as the text of their integer ids; no row carries a name (D-02). A day with more out-of-stock
hours 06-22 than `stockout.threshold` is a stockout (D-14); a discount at or below `unknown_price.below_or_equal` or
above `unknown_price.above` is an unknown price, flagged and counted (D-17). A negative sale, an out-of-stock count
outside 0..`stockout.hours_counted` or a value that is not a finite number is refused naming the row.

`profile` streams the rows once and reports what later plans size from: counts, codes per level, stores per city and
series per store, the split date ranges, the stockout and zero-sales shares, discounted and unknown-price rows, the
largest code of each level, and the units-factor evidence (the smallest distinct positive sale amounts and the share of
positive values on the smallest value's lattice; D-20).

The two parquet files are read by `read_files` (05.2-03): both are checked first, then `read_parquet` reads only the 17
reference columns (never the hourly arrays) in record batches, refuses a schema without one of them, and `read_files`
refuses a row count or a date outside the split's reference values. pyarrow is imported only inside `read_parquet`, so
the app and the tests never need it (D-07); everything else is pure and tested on CSV. No retailer or dataset value is
a code literal: every value comes from reference/public.json through `data.public_config`.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import math
import pathlib
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from . import config, public_config

CHUNK = 1 << 20  # 1 MiB reads for the checksum
BATCH_ROWS = 200_000  # rows per parquet record batch: memory stays bounded by the batch, not the file
FIRST_VALUES = 50  # how many distinct positive sale amounts the units-factor evidence lists (research Code Examples)
LATTICE_REL_TOL = 1e-6  # relative distance to an integer multiple of the smallest positive value (research A10)
CODES = (("city", "city_id"), ("store", "store_id"), ("mgmt", "management_group_id"), ("cat1", "first_category_id"),
         ("cat2", "second_category_id"), ("cat3", "third_category_id"), ("product", "product_id"))
FLOATS = (("precip", "precpt"), ("temp", "avg_temperature"), ("humidity", "avg_humidity"), ("wind", "avg_wind_level"))
FLAGS = (("holiday", "holiday_flag"), ("activity", "activity_flag"))
SCALARS = ("dt", "sale_amount", "discount")  # the stockout column comes from reference `stockout.column`
CATEGORY_LEVELS = ("mgmt", "cat1", "cat2", "cat3")
DEPTH_BUCKETS = (10, 20, 30)  # report buckets of percent off (0-10, 10-20, 20-30, over 30; plan 05.2-03), not a rule


class PublicDataError(ValueError):
    pass


def sha256_of(path: pathlib.Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(path: pathlib.Path | str, expected_sha256: str, expected_bytes: int) -> None:
    """Refuse `path` unless its size and sha256 are the expected ones. Nothing is parsed here or before this."""
    path = pathlib.Path(path)
    if not path.is_file():
        raise PublicDataError(f"{path.name}: not a file")
    size = path.stat().st_size
    if size != expected_bytes:
        raise PublicDataError(f"{path.name}: {size} bytes, expected {expected_bytes} bytes; refused before reading")
    actual = sha256_of(path)
    if actual != expected_sha256:
        raise PublicDataError(f"{path.name}: sha256 {actual} is not the expected {expected_sha256}; refused before "
                              "reading")


def verify_known(path: pathlib.Path | str, cfg: dict) -> None:
    """Verify `path` against the `dataset.files` entry that names it; a file no entry names is refused."""
    path = pathlib.Path(path)
    entries = {(f["sha256"], f["bytes"]) for f in cfg["dataset"]["files"].values() if f["name"] == path.name}
    if len(entries) != 1:
        raise PublicDataError(f"{path.name}: not a file reference/public.json dataset.files names"
                              if not entries else f"{path.name}: dataset.files names it with different checksums")
    verify(path, *entries.pop())


def read_csv(path: pathlib.Path | str, columns) -> object:
    """Yield each row of a checked CSV as a dict holding exactly `columns` (text values)."""
    path = pathlib.Path(path)
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, None) or []
        missing = [c for c in columns if c not in header]
        if missing:
            raise PublicDataError(f"{path.name}: missing column(s) {', '.join(missing)}")
        at = [header.index(c) for c in columns]
        for number, line in enumerate(reader, 2):
            if len(line) != len(header):
                raise PublicDataError(f"{path.name}: line {number} has {len(line)} fields, the header {len(header)}")
            yield {c: line[i] for c, i in zip(columns, at)}


def read_parquet(path: pathlib.Path | str, columns, batch_rows: int = BATCH_ROWS):
    """Yield each row of a parquet file as a dict holding exactly `columns`, read in record batches with column
    projection (the hourly arrays are never read). `dt` comes out as an ISO date string. A missing column is refused by
    name before any row is read. Call it only on a file that passed `verify`."""
    import pyarrow.parquet as pq  # only here: .venv-data has pyarrow, the app and the tests do not (D-07)

    path, columns = pathlib.Path(path), list(columns)
    parquet = pq.ParquetFile(path)
    names = set(parquet.schema_arrow.names)
    missing = [c for c in columns if c not in names]
    if missing:
        raise PublicDataError(f"{path.name}: missing column(s) {', '.join(missing)}")
    for batch in parquet.iter_batches(columns=columns, batch_size=batch_rows):
        for row in batch.to_pylist():
            value = row.get("dt")
            if isinstance(value, datetime):
                row["dt"] = value.date().isoformat()
            elif isinstance(value, date):
                row["dt"] = value.isoformat()
            yield row


def read_files(raw_dir: pathlib.Path | str, cfg: dict, stats: dict | None = None):
    """Check every `dataset.files` entry in `raw_dir` (size and sha256), then yield every row of each file in the
    reference's order (train, then eval). A file whose row count is not its `rows`, or a row dated outside its
    `first_dt`..`last_dt`, is refused naming the file. When `stats` is a dict, each finished file adds
    {split: {name, rows, first_dt, last_dt}} as measured."""
    raw_dir, files = pathlib.Path(raw_dir), cfg["dataset"]["files"]
    for entry in files.values():
        verify_known(raw_dir / entry["name"], cfg)
    columns = cfg["dataset"]["columns"]
    for split, entry in files.items():
        name, first, last = entry["name"], date.fromisoformat(entry["first_dt"]), date.fromisoformat(entry["last_dt"])
        n, low, high = 0, None, None
        for raw in read_parquet(raw_dir / name, columns):
            day = _dt(raw)
            if not first <= day <= last:
                raise PublicDataError(f"{name}: {_where(raw)}: dt {day.isoformat()} is outside {split} "
                                      f"{entry['first_dt']}..{entry['last_dt']}")
            low, high = min(low or day, day), max(high or day, day)
            n += 1
            yield raw
        if n != entry["rows"]:
            raise PublicDataError(f"{name}: {n} rows, expected {entry['rows']}")
        if stats is not None:
            stats[split] = {"name": name, "rows": n, "first_dt": low and low.isoformat(),
                            "last_dt": high and high.isoformat()}


SORT_COLUMNS = ("store_id", "product_id", "dt")  # series order: store code, product code (as integers), then date


def series_key(row: dict) -> tuple:
    """((city, store, product, (mgmt, cat1, cat2, cat3))) of a day row: one series' identity, codes only."""
    return (row["city"], row["store"], row["product"], tuple(row[level] for level in CATEGORY_LEVELS))


def group_series(rows):
    """Yield (series key, [day rows]) for consecutive day rows of one (store, product); rows must come sorted by
    store, product and day. A series that comes back after another one started is refused, never merged silently."""
    seen, key, chunk = set(), None, []
    for row in rows:
        k = series_key(row)
        if k != key:
            if chunk:
                yield key, chunk
            if (k[1], k[2]) in seen:
                raise PublicDataError(f"series (store {k[1]}, product {k[2]}) is not contiguous: rows are not sorted")
            seen.add((k[1], k[2]))
            key, chunk = k, []
        if chunk and row["day"] <= chunk[-1]["day"]:
            raise PublicDataError(f"series (store {k[1]}, product {k[2]}): day {row['day']} after day "
                                  f"{chunk[-1]['day']}: rows are not sorted by day")
        chunk.append(row)
    if chunk:
        yield key, chunk


def _sorted_csv_rows(csv_path: pathlib.Path | str, cfg: dict) -> list[dict]:
    rows = list(day_rows(read_csv(csv_path, cfg["dataset"]["columns"]), cfg))
    return sorted(rows, key=lambda r: (int(r["store"]), int(r["product"]), r["day"]))


def series_iter(raw_dir: pathlib.Path | str | None, cfg: dict, csv_path: pathlib.Path | str | None = None,
                batch_rows: int = BATCH_ROWS):
    """Yield ((city, store, product, cats), [day rows in day order]) one series at a time, in (store, product) code
    order (05.2-05; D-11: never every series' rows as Python objects at once).

    From `raw_dir`: both files are checked first (size, sha256), then the 17 reference columns are read into one
    pyarrow Table (train, then eval), each file's row count and date range checked, the table sorted by store,
    product and date, and turned into Python rows one record batch at a time; memory is the table plus one batch.
    From `csv_path` (tests and the fixture build): a checked CSV in the same schema, sorted in memory."""
    if csv_path is not None:
        verify_known(csv_path, cfg)
        yield from group_series(_sorted_csv_rows(csv_path, cfg))
        return
    import pyarrow as pa  # only here: .venv-data has pyarrow, the app and the tests do not (D-07)
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    raw_dir, files = pathlib.Path(raw_dir), cfg["dataset"]["files"]
    for entry in files.values():
        verify_known(raw_dir / entry["name"], cfg)
    columns = list(cfg["dataset"]["columns"])
    tables = []
    for split, entry in files.items():
        path = raw_dir / entry["name"]
        missing = [c for c in columns if c not in set(pq.ParquetFile(path).schema_arrow.names)]
        if missing:
            raise PublicDataError(f"{path.name}: missing column(s) {', '.join(missing)}")
        table = pq.read_table(path, columns=columns)
        if table.num_rows != entry["rows"]:
            raise PublicDataError(f"{path.name}: {table.num_rows} rows, expected {entry['rows']}")
        bounds = pc.min_max(table["dt"]).as_py()
        low, high = (_dt({"dt": bounds[k]}) for k in ("min", "max"))
        if low < date.fromisoformat(entry["first_dt"]) or high > date.fromisoformat(entry["last_dt"]):
            raise PublicDataError(f"{path.name}: dt {low}..{high} is outside {split} "
                                  f"{entry['first_dt']}..{entry['last_dt']}")
        tables.append(table)
    table = pa.concat_tables(tables).sort_by([(c, "ascending") for c in SORT_COLUMNS])
    del tables

    def raw_rows():
        for batch in table.to_batches(max_chunksize=batch_rows):
            yield from batch.to_pylist()

    yield from group_series(day_rows(raw_rows(), cfg))


def _where(raw: dict) -> str:
    return f"row (store {raw.get('store_id')}, product {raw.get('product_id')}, dt {raw.get('dt')})"


def _code(raw: dict, column: str) -> str:
    value = raw.get(column)
    if isinstance(value, bool):
        value = None
    try:
        number = int(value) if not isinstance(value, float) or value.is_integer() else None
    except (TypeError, ValueError):
        number = None
    if number is None or number < 0:
        raise PublicDataError(f"{_where(raw)}: {column} {value!r} is not an integer code")
    return str(number)


def _float(raw: dict, column: str) -> float:
    value = raw.get(column)
    try:
        number = float(value) if not isinstance(value, bool) else math.nan
    except (TypeError, ValueError):
        number = math.nan
    if not math.isfinite(number):
        raise PublicDataError(f"{_where(raw)}: {column} {value!r} is not a finite number")
    return number


def _whole(raw: dict, column: str) -> int:
    number = _float(raw, column)
    if not number.is_integer():
        raise PublicDataError(f"{_where(raw)}: {column} {raw.get(column)!r} is not a whole number")
    return int(number)


def _dt(raw: dict) -> date:
    value = raw.get("dt")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise PublicDataError(f"{_where(raw)}: dt {value!r} is not an ISO date") from None


def _check_columns(cfg: dict) -> None:
    known = {c for _, c in CODES + FLOATS + FLAGS} | set(SCALARS) | {cfg["stockout"]["column"]}
    if set(cfg["dataset"]["columns"]) != known:
        raise PublicDataError(f"dataset.columns: {sorted(set(cfg['dataset']['columns']) ^ known)} differ from the "
                              "columns day_rows reads")


def day_rows(raw_rows, cfg: dict):
    """Yield one typed day row per raw row: codes as text, the day index on the shifted calendar, the raw sale amount,
    the out-of-stock hours with the stockout flag, the discount with the unknown-price flag, the holiday and activity
    flags and the weather. Refusals name the row's (store, product, dt)."""
    _check_columns(cfg)
    cal = cfg["calendar"]
    shift, day0 = timedelta(days=cal["shift_days"]), date.fromisoformat(cal["day0"])
    first_day, last_day = cal["first_day"], cal["last_day"]
    oos_column, threshold, hours = cfg["stockout"]["column"], cfg["stockout"]["threshold"], cfg["stockout"][
        "hours_counted"]
    low, high = cfg["unknown_price"]["below_or_equal"], cfg["unknown_price"]["above"]
    for raw in raw_rows:
        row = {key: _code(raw, column) for key, column in CODES}
        source = _dt(raw)
        day = (source + shift - day0).days
        if not first_day <= day <= last_day:
            raise PublicDataError(f"{_where(raw)}: day {day} is outside calendar.first_day..last_day "
                                  f"({first_day}..{last_day})")
        sale = _float(raw, "sale_amount")
        if sale < 0:
            raise PublicDataError(f"{_where(raw)}: sale_amount {sale} is negative")
        oos = _whole(raw, oos_column)
        if not 0 <= oos <= hours:
            raise PublicDataError(f"{_where(raw)}: {oos_column} {oos} is outside 0..{hours}")
        discount = _float(raw, "discount")
        row.update(day=day, dt=source.isoformat(), sale_amount=sale, oos_hours=oos, stockout=oos > threshold,
                   discount=discount, unknown_price=discount <= low or discount > high)
        for key, column in FLAGS:
            flag = _whole(raw, column)
            if flag not in (0, 1):
                raise PublicDataError(f"{_where(raw)}: {column} {flag} is not 0 or 1")
            row[key] = flag
        for key, column in FLOATS:
            row[key] = _float(raw, column)
        yield row


def _spread(counts, p90: bool = False) -> dict:
    """min, median, max (and the nearest-rank 90th percentile when asked) of `counts`; None for each when empty."""
    values = sorted(counts)
    if not values:
        return dict.fromkeys(("min", "median", "p90", "max") if p90 else ("min", "median", "max"))
    out = {"min": values[0], "median": statistics.median(values)}
    if p90:
        out["p90"] = values[math.ceil(0.9 * len(values)) - 1]
    out["max"] = values[-1]
    return out


def _by_code(mapping: dict) -> dict:
    return {code: mapping[code] for code in sorted(mapping, key=int)}


def _depth_bucket(discount: float, full_price: float) -> str:
    depth = round((full_price - discount) * 100, 6)  # percent off; rounded so 1 - 0.7 is 30, not 30.000000000000004
    for upper in DEPTH_BUCKETS:
        if depth <= upper:
            return f"{upper - 10}-{upper}"
    return f"over_{DEPTH_BUCKETS[-1]}"


def profile(rows, cfg: dict) -> dict:
    """One pass over day rows: what the dataset holds, for sizing, for the 05.2-06 decisions and for the README's limits.
    Never keeps the rows: memory is bounded by the number of series and of distinct positive sale amounts."""
    long_hours, full_price = cfg["stockout"]["long_hours"], cfg["discount"]["full_price"]
    low = cfg["unknown_price"]["below_or_equal"]
    n = stockout = long_days = zero = zero_in_stock = discounted = unknown_low = unknown_high = 0
    distinct = {key: set() for key, _ in CODES}
    city_stores, store_series, city_of = defaultdict(set), defaultdict(set), {}
    category_series = {level: set() for level in CATEGORY_LEVELS}  # (code, store, product) per level
    store_rows, store_stockouts = Counter(), Counter()
    depth = Counter({f"{upper - 10}-{upper}": 0 for upper in DEPTH_BUCKETS} | {f"over_{DEPTH_BUCKETS[-1]}": 0})
    splits = {}
    positives = Counter()
    for row in rows:
        n += 1
        for key, _ in CODES:
            distinct[key].add(row[key])
        store, product = row["store"], row["product"]
        city_stores[row["city"]].add(store)
        store_series[store].add(product)
        city_of[store] = row["city"]
        for level in CATEGORY_LEVELS:
            category_series[level].add((row[level], store, product))
        split = splits.setdefault("train" if row["day"] < 0 else "eval",
                                  {"rows": 0, "first_dt": row["dt"], "last_dt": row["dt"],
                                   "first_day": row["day"], "last_day": row["day"]})
        split["rows"] += 1
        split["first_dt"], split["last_dt"] = min(split["first_dt"], row["dt"]), max(split["last_dt"], row["dt"])
        split["first_day"], split["last_day"] = min(split["first_day"], row["day"]), max(split["last_day"], row["day"])
        store_rows[store] += 1
        if row["stockout"]:
            stockout += 1
            store_stockouts[store] += 1
        long_days += row["oos_hours"] >= long_hours
        if row["sale_amount"] == 0:
            zero += 1
            zero_in_stock += not row["stockout"]
        else:
            positives[row["sale_amount"]] += 1
        if row["unknown_price"]:
            if row["discount"] <= low:
                unknown_low += 1
            else:
                unknown_high += 1
        elif row["discount"] < full_price:
            discounted += 1
            depth[_depth_bucket(row["discount"], full_price)] += 1
    in_stock = n - stockout
    by_city_series = Counter()
    for store, products in store_series.items():
        by_city_series[city_of[store]] += len(products)
    categories = {}
    for level, triples in category_series.items():
        per_code, per_series = Counter(), Counter()
        for code, store, product in triples:
            per_code[code] += 1
            per_series[(store, product)] += 1
        categories[level] = {"codes": len(per_code), "series_per_code": _spread(per_code.values()),
                             "series_on_two_codes": sum(1 for c in per_series.values() if c > 1)}
    smallest = min(positives) if positives else None
    positive_rows = sum(positives.values())
    on_lattice = 0
    if smallest:
        for value, count in positives.items():
            k = round(value / smallest)
            on_lattice += count if k >= 1 and abs(value - k * smallest) <= LATTICE_REL_TOL * value else 0
    dataset = cfg["dataset"]
    return {
        "note": f"Profile of the public {dataset['name']} files ({dataset['license']}, {dataset['creator']}); "
                "aggregates only; figures describe the public data, not any retailer.",
        "rows": n,
        "series": sum(len(p) for p in store_series.values()),
        "stores": len(distinct["store"]),
        "cities": len(distinct["city"]),
        "products": len(distinct["product"]),
        "splits": splits,
        "categories": categories,
        "spread": {
            "stores_per_city": {**_spread((len(s) for s in city_stores.values()), p90=True),
                                "by_city": _by_code({c: len(s) for c, s in city_stores.items()})},
            "series_per_store": {**_spread((len(p) for p in store_series.values()), p90=True),
                                 "by_city": _by_code(dict(by_city_series))},
        },
        "stockout": {"days": stockout, "share": stockout / n if n else None,
                     "long_days": long_days, "long_share": long_days / n if n else None,
                     "per_store_share": _spread(store_stockouts[s] / store_rows[s] for s in store_rows)},
        "zero_sales": {"rows": zero, "share": zero / n if n else None, "in_stock_rows": zero_in_stock,
                       "in_stock_share": zero_in_stock / in_stock if in_stock else None},
        "discount": {"rows": discounted, "share": discounted / n if n else None, "depth": dict(depth)},
        "unknown_price": {"rows": unknown_low + unknown_high, "at_or_below": unknown_low, "above": unknown_high},
        "codes": {"max_id": {key: max((int(c) for c in codes), default=None) for key, codes in distinct.items()},
                  "stores_by_city": _by_code({c: sorted(s, key=int) for c, s in city_stores.items()})},
        "units_factor": {
            "smallest_positive": smallest,
            "lattice_share": on_lattice / positive_rows if positives else None,
            "positive_rows": positive_rows,
            "distinct_positive_values": len(positives),
            "first_values": sorted({round(v, 6) for v in positives})[:FIRST_VALUES],
        },
    }


def _summary(report: dict) -> str:
    return (f"profile: {report['series']} series, {report['stores']} stores, {report['cities']} cities, "
            f"{report['products']} products")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="frn.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("verify", help="check both dataset files and count their rows and dates")
    check.add_argument("--raw", required=True, type=pathlib.Path, help="the folder holding the dataset files")
    prof = sub.add_parser("profile", help="profile the dataset files (--raw) or a checked CSV in their schema (--csv)")
    source = prof.add_mutually_exclusive_group(required=True)
    source.add_argument("--raw", type=pathlib.Path, help="the folder holding the dataset files")
    source.add_argument("--csv", type=pathlib.Path)
    prof.add_argument("--out", type=pathlib.Path, help="write the profile JSON here and print a one-line summary")
    for each in (check, prof):
        each.add_argument("--fixture", type=pathlib.Path,
                          help="a fixture's override JSON (dataset and calendar blocks)")
    args = parser.parse_args(argv)
    try:
        cfg = public_config.load_for(args.fixture) if args.fixture else public_config.load()
        if args.command == "verify":
            stats = {}
            for _ in read_files(args.raw, cfg, stats):
                pass
            for s in stats.values():
                print(f"ok {s['name']} {s['rows']} rows {s['first_dt']}..{s['last_dt']}")
            return 0
        if args.raw is not None:
            rows = read_files(args.raw, cfg)
        else:
            verify_known(args.csv, cfg)
            rows = read_csv(args.csv, cfg["dataset"]["columns"])
        report = profile(day_rows(rows, cfg), cfg)
    except (PublicDataError, config.ConfigError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    if args.out is None:
        print(config.canonical(report))
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        config.atomic_write(args.out, config.canonical(report) + "\n")
        print(_summary(report))
    return 0

if __name__ == "__main__":
    sys.exit(main())
