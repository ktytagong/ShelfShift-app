"""The reference set (data.reference): one dataset_version over seed, forecast and truth; check() is the horizon
coverage gate; release_check() is the deploy gate that refuses the local stand-in (DATA-05, D-09).

Every set here is built in tmp_path from the committed seed.json and truth.json and a forecast built in the test,
never from the committed forecast.json (which becomes the user's BigQuery forecast in 03-11/03-12). The "BigQuery"
forecast is built by data.forecast.build with fake providers named ai_forecast and arima_plus: no cloud call."""
import json
import re
import shutil

import pytest

from data import config
from data import forecast as F
from data import reference as R
from data import versions


def _committed(name: str) -> dict:
    return json.loads((R.REF / f"{name}.json").read_text(encoding="utf-8"))


def fake_valid(inputs_by_series: dict, required_days: dict) -> dict:
    """A well-behaved provider: the mean of the last 7 input values, a ±20% interval, confidence 0.8, empty status."""
    out = {}
    for sid, days in required_days.items():
        last = [v for _, v, _ in inputs_by_series[sid]][-7:]
        base = sum(last) / len(last)
        out[sid] = [{"day": d, "low": base * 0.8, "base": base, "high": base * 1.2,
                     "confidence_level": 0.8, "status": ""} for d in days]
    return out


@pytest.fixture(scope="module")
def seed() -> dict:
    return _committed("seed")


@pytest.fixture(scope="module")
def crossed(seed) -> str:
    """The one demo series the fake primary returns crossed (low above high), so the fallback is exercised."""
    return F.series_id("d3", seed["branches"][0], seed["products"][0]["sku"])


@pytest.fixture(scope="module")
def local_forecast(seed) -> dict:
    return F.build(seed, F.LOCAL_PROVIDER)


@pytest.fixture(scope="module")
def release_forecast(seed, crossed) -> dict:
    def primary(inputs_by_series, required_days):
        out = fake_valid(inputs_by_series, required_days)
        out[crossed] = [dict(r, low=r["high"] + 1.0) for r in out[crossed]]
        return out

    return F.build(seed, F.Provider(F.AI_FORECAST, primary), F.Provider(F.ARIMA_PLUS, fake_valid))


def _write_set(path, forecast: dict):
    path.mkdir()
    for name in ("seed", "truth"):
        shutil.copyfile(R.REF / f"{name}.json", path / f"{name}.json")
    (path / "forecast.json").write_text(F.dumps(forecast), encoding="utf-8")
    return path


@pytest.fixture
def ref_dir(tmp_path, local_forecast):
    """A complete set whose forecast is the local stand-in (built here, not the committed file)."""
    return _write_set(tmp_path / "local", local_forecast)


@pytest.fixture
def release_dir(tmp_path, release_forecast):
    """A complete all-BigQuery set: fake ai_forecast for every series but one, fake arima_plus for that one."""
    return _write_set(tmp_path / "release", release_forecast)


def read(path, name: str) -> dict:
    return json.loads((path / f"{name}.json").read_text(encoding="utf-8"))


def write(path, name: str, payload: dict) -> None:
    (path / f"{name}.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")


def test_a_complete_reference_set_passes_check(ref_dir):
    ref = R.load(ref_dir)
    R.check(ref)
    assert re.fullmatch(r"ds-[0-9a-f]{12}", ref["dataset_version"])
    assert set(ref) == {"dataset_version", "seed", "forecast", "truth"}
    committed = R.load()  # the committed set: complete whatever its provider
    R.check(committed)
    assert committed["dataset_version"].startswith("ds-")


def test_the_local_stand_in_is_refused_at_release(ref_dir, seed):
    ref = R.load(ref_dir)
    n = len(ref["forecast"]["points"])
    first = F.series_id("d0", min(seed["branches"]), min(p["sku"] for p in seed["products"]))
    with pytest.raises(R.ReleaseError, match=re.escape(f"refusing the local stand-in: {n} points (first {first} "
                                                       f"day 0); run python -m data.forecast --provider ai_forecast")):
        R.release_check(ref)


def test_a_valid_bigquery_set_passes_release(release_dir, seed, crossed):
    ref = R.load(release_dir)
    providers = {p["provider"] for p in ref["forecast"]["points"]}
    assert providers == {F.AI_FORECAST, F.ARIMA_PLUS}
    _, branch, sku = crossed.split("|")
    d3 = next(r for r in ref["forecast"]["runs"] if r["job"] == "d3")
    assert d3["fallbacks"] == [{"branch": branch, "sku": sku, "failed_check": "bounds", "detail": d3["fallbacks"][0]
                                ["detail"]}]
    info = R.release_check(ref)
    total = ref["forecast"]["provider_summary"]["demo"]["series_runs"]
    assert info == {
        "dataset_version": ref["dataset_version"],
        "headline": f"{F.PROVIDERS[F.AI_FORECAST]} for {total - 1} of {total} series-runs; ARIMA_PLUS fallback "
                    f"for 1 (failed interval checks)",
        "seed_input_version": seed["dataset"]["input_version"],
        "forecast_input_version": seed["dataset"]["input_version"],
        "note": "",
    }


def test_an_unknown_provider_is_refused(release_dir):
    fc = read(release_dir, "forecast")
    fc["points"][5]["provider"] = "prophet"
    write(release_dir, "forecast", fc)
    with pytest.raises(R.ReleaseError, match=r"refusing provider 'prophet': 1 point \(first .+\); a release "
                                             r"forecast comes only from ai_forecast or arima_plus"):
        R.release_check(R.load(release_dir))


def test_the_cli_prints_ok_or_failed(ref_dir, release_dir, capsys):
    assert R.main(["--ref", str(ref_dir)]) == 0
    assert re.match(r"ok ds-[0-9a-f]{12} \(local seasonal-naive stand-in", capsys.readouterr().out)
    assert R.main(["--ref", str(ref_dir), "--release"]) == 1
    assert capsys.readouterr().err.startswith("FAILED: refusing the local stand-in: ")
    assert R.main(["--ref", str(release_dir), "--release"]) == 0
    out = capsys.readouterr().out
    assert re.match(r"ok ds-[0-9a-f]{12} \(TimesFM 2\.5 via BigQuery AI\.FORECAST for ", out)
    assert "note:" not in out


# --- the gate's edges (Task 2) -----------------------------------------------------------------------------------

def _drop_last_by_day(rows: list) -> None:
    rows.sort(key=lambda r: r["day"])
    rows.pop()


@pytest.mark.parametrize("part, key, message", [
    ("forecast", "points", r"forecast run fr-\S+ has no \S+ at .+ for day 16$"),
    ("truth", "demand", r"^no hidden demand for \S+ at .+ on day 16$"),
    ("seed", "capacity", r"^no (chilled|ambient) capacity for .+ on day 16$"),
])
def test_a_missing_date_fails_loudly(ref_dir, part, key, message):
    data = read(ref_dir, part)
    _drop_last_by_day(data[key])
    write(ref_dir, part, data)
    with pytest.raises(R.CoverageError, match=message):
        R.check(R.load(ref_dir))


@pytest.mark.parametrize("group, id_key", [("lots", "lot_id"), ("inbound", "inbound_id")])
def test_a_lot_expiring_on_the_horizon_end_passes_and_one_day_later_fails(ref_dir, group, id_key):
    seed = read(ref_dir, "seed")
    end = seed["calendar"]["horizon_end"]
    row = seed[group][0]
    row["expiry_day"] = end
    write(ref_dir, "seed", seed)
    R.check(R.load(ref_dir))
    row["expiry_day"] = end + 1
    write(ref_dir, "seed", seed)
    with pytest.raises(R.CoverageError, match=f"^{re.escape(row[id_key])} expires after the horizon end {end}$"):
        R.check(R.load(ref_dir))


def test_an_empty_points_list_names_the_first_gap(ref_dir, release_dir, seed):
    first = f"has no {seed['products'][0]['sku']} at {seed['branches'][0]} for day 0$"
    for path, gate in ((ref_dir, R.check), (release_dir, R.release_check)):
        fc = read(path, "forecast")
        d0 = next(r["forecast_run_id"] for r in fc["runs"] if r["job"] == "d0")
        fc["points"] = []
        write(path, "forecast", fc)
        with pytest.raises(R.CoverageError, match=f"^forecast run {d0} {first}"):
            gate(R.load(path))


def test_an_empty_runs_list_names_demo_day_0(ref_dir, release_dir):
    for path, gate in ((ref_dir, R.check), (release_dir, R.release_check)):
        fc = read(path, "forecast")
        fc["runs"] = []
        write(path, "forecast", fc)
        with pytest.raises(R.CoverageError, match="^no forecast run for demo day 0$"):
            gate(R.load(path))


def _reversed(obj):
    """The same JSON with every object's keys in reverse order (lists keep their order)."""
    if isinstance(obj, dict):
        return {k: _reversed(obj[k]) for k in reversed(list(obj))}
    if isinstance(obj, list):
        return [_reversed(x) for x in obj]
    return obj


@pytest.mark.parametrize("part, change", [
    ("seed", lambda s: s["capacity"][0].update(free_totes=s["capacity"][0]["free_totes"] + 1)),
    ("forecast", lambda f: f["points"][0].update(high=f["points"][0]["high"] + 0.01)),
    ("truth", lambda t: t["demand"][0].update(demand=t["demand"][0]["demand"] + 1)),
])
def test_key_order_and_indentation_do_not_change_the_version(release_dir, part, change):
    before = R.load(release_dir)["dataset_version"]
    for name in ("seed", "forecast", "truth"):
        text = json.dumps(_reversed(read(release_dir, name)), indent=4, ensure_ascii=False)
        (release_dir / f"{name}.json").write_text(text, encoding="utf-8")
    assert list(read(release_dir, "seed"))[0] == "settings"  # the rewrite really reversed the keys
    assert R.load(release_dir)["dataset_version"] == before
    data = read(release_dir, part)
    change(data)
    write(release_dir, part, data)
    assert R.load(release_dir)["dataset_version"] != before


def test_a_sales_history_change_fails_the_release(release_dir):
    R.release_check(R.load(release_dir))
    seed = read(release_dir, "seed")
    row = seed["daily_sales"][0]
    row["sold_full"] = row["recorded"] = row["recorded"] + 1.0
    write(release_dir, "seed", seed)
    with pytest.raises(R.ReleaseError, match="^the forecast was built from another sales history: "):
        R.release_check(R.load(release_dir))


def _restamp(path) -> str:
    """What `python -m data.seed` does after a change: a new input_version over the new bodies, in both files."""
    seed, truth = read(path, "seed"), read(path, "truth")
    version = versions.input_version(seed["dataset"]["reference_fingerprint"], seed, truth)
    seed["dataset"]["input_version"] = truth["input_version"] = version
    write(path, "seed", seed)
    write(path, "truth", truth)
    return version


def _r02(seed):
    run = next(r for r in seed["runs"] if r["run_code"] == "R-02")
    run["fixed_cost"] = round(run["fixed_cost"] + 5.0, 2)


@pytest.mark.parametrize("change", [
    _r02,
    lambda s: s["capacity"][3].update(free_totes=s["capacity"][3]["free_totes"] + 2),
    lambda s: s["lots"][0].update(last_verified_day=s["lots"][0]["last_verified_day"] - 1),
], ids=["run quote", "capacity", "verified day"])
def test_a_quote_change_keeps_the_forecast_valid_with_a_note(release_dir, seed, change, capsys):
    basis = versions.basis_id(seed)
    edited = read(release_dir, "seed")
    change(edited)
    write(release_dir, "seed", edited)
    moved = _restamp(release_dir)
    assert moved != seed["dataset"]["input_version"]
    ref = R.load(release_dir)
    assert versions.basis_id(ref["seed"]) == basis  # outside the forecast basis
    info = R.release_check(ref)
    assert info["seed_input_version"] == moved
    assert info["forecast_input_version"] == seed["dataset"]["input_version"]
    assert info["note"] == R.MOVED_NOTE
    assert R.main(["--ref", str(release_dir), "--release"]) == 0
    assert capsys.readouterr().out.splitlines()[1] == f"note: {R.MOVED_NOTE}"


def test_a_truth_from_another_seed_fails(release_dir, evaluation_pair):
    _, other = evaluation_pair
    write(release_dir, "truth", other)
    R.check(R.load(release_dir))  # complete, so only the release gate can tell
    with pytest.raises(R.ReleaseError, match=f"^truth.json is from another world: its input_version "
                                             f"'{other['input_version']}' is not seed.json's "):
        R.release_check(R.load(release_dir))


def test_an_evaluation_world_is_never_a_release(tmp_path, evaluation_pair):
    """A whole, consistent evaluation set (its own seed, truth and an all-BigQuery forecast built from it) is
    complete, and refused only because it is not the demo world."""
    seed, truth = evaluation_pair
    fc = F.build(seed, F.Provider(F.AI_FORECAST, fake_valid))
    for name, payload in (("seed", seed), ("truth", truth), ("forecast", fc)):
        write(tmp_path, name, payload)
    ref = R.load(tmp_path)
    R.check(ref)
    with pytest.raises(R.ReleaseError, match=r"^seed.json is the 'evaluation' world on seed \d+; a release is the "
                                             r"'demo' world on the reference demo seed \d+$"):
        R.release_check(ref)


@pytest.mark.parametrize("field, value", [("scenario", "evaluation"), ("seed", 1)])
def test_a_demo_set_relabelled_off_the_release_seed_fails(release_dir, field, value):
    seed = read(release_dir, "seed")
    seed["dataset"][field] = value
    write(release_dir, "seed", seed)
    _restamp(release_dir)  # as python -m data.seed would after the change, so only the demo-world rule can tell
    with pytest.raises(R.ReleaseError, match="a release is the 'demo' world on the reference demo seed"):
        R.release_check(R.load(release_dir))


# --- the gate binds seed.json to the shipped reference/ and to its own bodies (03-15, G-03-1, CR-01) -------------

def _lot_quantity(seed):
    lot = next((x for x in seed["lots"] if x["lot_id"] == "MLK-042"), None)
    assert lot is not None, "the designed lot MLK-042 is not in seed.json"
    lot["qty"] = 9999.0


def _price(seed):
    product = next((x for x in seed["products"] if x["sku"] == "MILK-1L"), None)
    assert product is not None, "MILK-1L is not in seed.json"
    product["price"] = 0.01


@pytest.mark.parametrize("part, change, kind", [
    ("seed", _lot_quantity, "body"),
    ("seed", _price, "body"),
    ("seed", lambda s: s["policy"].update(waste_price=99.0), "body"),
    ("truth", lambda t: t["demand"][0].update(demand=t["demand"][0]["demand"] + 1), "body"),
    ("seed", lambda s: s["dataset"].update(reference_fingerprint="ref-000000000000"), "fingerprint"),
], ids=["lot quantity", "price", "waste price", "hidden demand", "stored fingerprint"])
def test_a_tampered_release_set_is_refused(release_dir, seed, part, change, kind):
    """An edit outside the forecast basis keeps basis_id and passes check(); only the release gate can refuse it,
    by recomputing input_version over the bodies (body) or comparing the stored reference fingerprint (fingerprint)."""
    R.release_check(R.load(release_dir))  # the untouched set ships
    data = read(release_dir, part)
    change(data)
    write(release_dir, part, data)
    assert versions.basis_id(read(release_dir, "seed")) == versions.basis_id(seed)  # why the old gate missed it
    R.check(R.load(release_dir))  # complete, so only the release gate can tell
    if kind == "body":
        message = (r"^seed\.json/truth\.json bodies hash to 'in-[0-9a-f]{12}', not their stamped "
                   + re.escape(repr(seed["dataset"]["input_version"]))
                   + r"; they were edited after generation; regenerate both with python -m data\.seed$")
    else:
        message = "^" + re.escape(f"seed.json was generated from reference 'ref-000000000000', reference/ is now "
                                  f"{config.fingerprint()!r}; regenerate with python -m data.seed") + "$"
    with pytest.raises(R.ReleaseError, match=message):
        R.release_check(R.load(release_dir))
    assert R.main(["--ref", str(release_dir), "--release"]) == 1


def test_a_reference_edit_never_regenerated_is_refused(release_dir, tmp_path, seed, monkeypatch):
    """A reference/ value changed after seed.json was generated: the seed is complete and self-consistent, but it
    no longer comes from the reference/ the deploy ships. Only a copy is edited, never the real reference/."""
    ref_copy = tmp_path / "reference"
    shutil.copytree(config.REF_IN, ref_copy)
    policy = ref_copy / "policy.json"
    data = json.loads(policy.read_text(encoding="utf-8"))
    data["engine_policy"]["waste_price"]["value"] = 99.0
    policy.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    original = config.fingerprint
    edited = original(ref_copy)
    assert edited != original()
    # fingerprint's default REF_IN is bound at definition time, so the module attribute is patched, never REF_IN
    monkeypatch.setattr(config, "fingerprint", lambda ref_in=ref_copy: original(ref_in))
    R.check(R.load(release_dir))  # complete, so only the release gate can tell
    stored = seed["dataset"]["reference_fingerprint"]
    with pytest.raises(R.ReleaseError, match="^" + re.escape(
            f"seed.json was generated from reference {stored!r}, reference/ is now {edited!r}; regenerate with "
            f"python -m data.seed") + "$"):
        R.release_check(R.load(release_dir))
