"""The Day-3 story check (03-08; 03-CONTEXT D-15, D-18, D-21, D-23, D-24).

Every test here holds whether or not a SUS-19 beat holds on the release seed: a failing beat is a recorded result,
not a broken check. No SUS-19 unit number is pinned (D-15: the story is fixed, the numbers are whatever the release
seed gives); reference knobs are read through config, never written as literals.
"""
import copy
import json

import pytest

from data import config as C
from data import seed as S
from data import story as ST

REVIEWER_TAGS = set(ST.CASES)


@pytest.fixture(scope="module")
def cfg():
    return C.load()


@pytest.fixture(scope="module")
def committed():
    """(seed.json, truth.json, forecast.json) of the committed release reference set."""
    return ST._load(ST.REF)


@pytest.fixture(scope="module")
def report():
    """A fresh story report on the committed release reference set."""
    return ST.run()


@pytest.fixture(scope="module")
def tags(cfg):
    return {d["tag"]: lot_id for lot_id, d in cfg["scenarios"]["demo"]["designed_lots"].items()}


def inputs(release_world, committed, cfg, tags):
    seed_json, _, forecast_json = committed
    return {tag: ST.build_input(release_world, seed_json, forecast_json, cfg, tags[tag]) for tag in REVIEWER_TAGS}


def test_the_report_records_every_beat_and_reviewer_lot(report, tags):
    s = report["sus19"]
    assert set(s["beats"]) == set(ST.BEATS) and all(isinstance(v, bool) for v in s["beats"].values())
    assert set(report["reviewer_lots"]) == {tags[t] for t in REVIEWER_TAGS}
    for lot_id, v in report["reviewer_lots"].items():
        assert v["tag"] in REVIEWER_TAGS and v["target"] == ST.CASES[v["tag"]]
        assert isinstance(v["target_met"], bool)
        assert v["recommended"] and v["recommended_label"] and v["kind"] in ("KEEP", "MARKDOWN", "TRANSFER")
        assert 0 <= v["scenario_wins"]["wins"] <= 3
    day2 = report["lat_phrao_milk_recorded_day2"]
    assert isinstance(day2, (int, float)) and not isinstance(day2, bool)
    assert set(s["numbers"]) == set(s["about"])
    for key, value in s["numbers"].items():
        assert value is None or isinstance(value, (int, float)), key
        assert s["about"][key] == ST.about(value), key
    assert isinstance(s["video_phrase"], str) and s["video_phrase"].startswith("wins ")
    assert s["wins_on_record"] == s["on_record"]["scenario_wins"]["wins"]
    assert s["wins_after_count"] == s["after_count"]["scenario_wins"]["wins"]
    if s["on_record"]["decide_by"] is not None and s["on_record"]["markdown_step"] is not None:
        assert s["demo_line"].startswith("Decide by ") and "the price drops" in s["demo_line"]
    assert report["label"].startswith("Synthetic data · hypothetical scenario")
    assert report["input_version"].startswith("in-") and report["basis_id"].startswith("fb-")
    assert report["forecast_providers"] and all(n > 0 for n in report["forecast_providers"].values())


def test_about_rounds_half_up_to_10_from_100_and_to_5_from_10():
    assert [ST.about(x) for x in (145, 144.9, 100, 99.9, 12.5, 12.49, 10, 9.5, 9.49, 0)] == \
        [150, 140, 100, 100, 15, 10, 10, 10, 9, 0]
    assert ST.about(None) is None


def test_every_reviewer_lot_meets_its_first_step_on_day_4(report, cfg):
    for lot_id, v in report["reviewer_lots"].items():
        schedule = C.product_schedule(cfg, v["sku"])
        assert v["first_step_day"] == v["expiry_day"] - max(k for k, _ in schedule), lot_id
        assert v["first_step_day"] == ST.FIRST_STEP_DAY == 4, lot_id
    assert all("first step" not in line for line in ST.failures(report))


def test_strawberry_transfers_are_rejected_for_cold_chain(release_world, committed, cfg, tags):
    inp = inputs(release_world, committed, cfg, tags)["strawberry-cold-chain"]
    assert inp.product.zone == "chilled" and inp.runs and not any(r.cold_chain for r in inp.runs)
    result = ST.evaluate_case(inp)["result"]
    rows = [r for r in result["rejected"] if r["run_code"] is not None]
    assert {r["run_code"] for r in rows} == {r.run_code for r in inp.runs}
    assert all(ST.COLD_CHAIN in r["reasons"] for r in rows)
    assert all(o["kind"] != "TRANSFER" for o in result["options"])


def test_the_story_reads_the_record_not_physical_stock(release_world, committed, cfg, tags):
    seed_json, truth, _ = committed
    phantom = tags[ST.PHANTOM_TAG]
    inp = ST.build_input(release_world, seed_json, committed[2], cfg, tags[ST.STORY_TAG])
    lot = next(l for l in inp.lots if l.lot_id == phantom)
    record = next(l["recorded"] for l in release_world.snapshots[ST.TODAY] if l["lot_id"] == phantom)
    assert truth["physical"][phantom] == 0.0
    assert lot.qty == record == cfg["scenarios"]["demo"]["designed_lots"][phantom]["extra"] > 0
    # every lot in the input is the record at 09:00, never truth.json's physical stock
    by_id = {l["lot_id"]: l["qty"] for l in ST.day3_lots(release_world, seed_json, inp.product.sku)}
    assert all(l.qty == by_id[l.lot_id] for l in inp.lots)
    # the count enters as a reported stock_count fact; the lot itself is unchanged
    counted = ST.with_count(inp, phantom, 0.0, ST.TODAY)
    assert counted.reported_facts == ({"kind": "stock_count", "lot_id": phantom, "units": 0.0,
                                       "counted_day": ST.TODAY},)
    assert counted.lots == inp.lots and inp.reported_facts == ()


def test_todays_0600_deliveries_are_lots_and_only_todays_runs_are_offered(release_world, committed, cfg, tags):
    seed_json = committed[0]
    for tag, inp in inputs(release_world, committed, cfg, tags).items():
        assert all(i.arrival_day > ST.TODAY for i in inp.inbound), tag  # D-21: no inbound arriving today
        assert all(r.origin == inp.source and r.day == ST.TODAY for r in inp.runs), tag  # D-24
        branches = {inp.source} | {r.dest for r in inp.runs}
        today = {r["lot_id"] for r in release_world.deliveries
                 if r["sku"] == inp.product.sku and r["day"] == ST.TODAY and r["qty"] > 0 and r["branch"] in branches}
        assert today <= {l.lot_id for l in inp.lots}, tag
        assert {l["lot_id"] for l in ST.day3_lots(release_world, seed_json, inp.product.sku)} >= today, tag
    assert any(r["day"] == ST.TODAY and r["qty"] > 0 for r in release_world.deliveries)  # the rule is exercised


def test_compare_names_a_changed_outcome(report, tags):
    assert ST.compare(report, copy.deepcopy(report)) == []
    changed = copy.deepcopy(report)
    lot_id = tags["bread-keep"]
    changed["reviewer_lots"][lot_id]["recommended"] = "something-else"
    changed["reviewer_lots"][lot_id]["target_met"] = not report["reviewer_lots"][lot_id]["target_met"]
    beat = ST.BEATS[1]
    changed["sus19"]["beats"][beat] = not report["sus19"]["beats"][beat]
    lines = ST.compare(report, changed)
    assert len(lines) == 3
    assert any(lot_id in line and "recommended" in line and "something-else" in line for line in lines)
    assert any(lot_id in line and "target_met" in line for line in lines)
    assert any(beat in line for line in lines)


def test_the_committed_story_matches_a_fresh_run(report):
    assert ST.dumps(report) == (ST.REF / "reports" / "story.json").read_text(encoding="utf-8")


def test_every_reviewer_case_records_its_fact_checks(report, release_world, committed, cfg, tags):
    keys = {"fact_id", "kind", "lot_ids", "pick_changed", "pick_feasible_if_alt", "changes_plan", "regret"}
    built = inputs(release_world, committed, cfg, tags)
    for lot_id, v in report["reviewer_lots"].items():
        assert isinstance(v["fact_checks"], list), lot_id
        assert all(set(c) == keys for c in v["fact_checks"]), lot_id
        engine_checks = ST.evaluate_case(built[v["tag"]])["fact_checks"]
        assert v["fact_checks"] == [{k: c[k] for k in keys} for c in engine_checks], lot_id


def test_competing_lots_are_other_early_lots_of_the_same_pair(report, release_world, committed, cfg):
    seed_json = committed[0]
    for lot_id, v in report["reviewer_lots"].items():
        schedule = C.product_schedule(cfg, v["sku"])
        expected = sorted(l["lot_id"] for l in ST.day3_lots(release_world, seed_json, v["sku"])
                          if l["branch"] == v["branch"] and l["lot_id"] != lot_id and l["qty"] > 0
                          and ST.first_step_day(l["expiry_day"], schedule) <= ST.FIRST_STEP_DAY)
        assert sorted(c["lot_id"] for c in v["competing_lots"]) == expected, lot_id
        for c in v["competing_lots"]:
            assert c["lot_id"] != lot_id and c["qty"] > 0 and c["first_step_day"] <= ST.FIRST_STEP_DAY
            assert c["origin"] in ("designed", "natural")


def test_the_cli_writes_the_report_and_exits_by_the_beats(report, tmp_path, capsys):
    out = tmp_path / "story.json"
    code = ST.main(["--out", str(out)])
    assert out.read_text(encoding="utf-8") == ST.dumps(report)
    assert code == (1 if ST.failures(report) else 0)
    printed = capsys.readouterr().out
    assert "beats: " in printed and "demo line: " in printed
    saved = copy.deepcopy(report)
    first = sorted(saved["reviewer_lots"])[0]
    saved["reviewer_lots"][first]["recommended"] = "an-older-plan"
    old = tmp_path / "old.json"
    old.write_text(json.dumps(saved), encoding="utf-8")
    code = ST.main(["--out", str(tmp_path / "again.json"), "--compare", str(old)])
    assert code == (1 if ST.failures(report) else 3)
    if not ST.failures(report):
        assert f"CHANGED {first} recommended" in capsys.readouterr().out


def test_sus19_beats_hold_on_the_release_seed(report):
    """D-15's beats hold on the release seed (03-08 Task 2: the story CLI exited 0); no unit number is pinned."""
    committed = json.loads((ST.REF / "reports" / "story.json").read_text(encoding="utf-8"))
    assert set(committed["sus19"]["beats"]) == set(ST.BEATS)
    assert all(committed["sus19"]["beats"].values()), committed["sus19"]["beats"]
    assert report["sus19"]["beats"] == committed["sus19"]["beats"]


def test_lat_phrao_recorded_no_milk_on_day_2(report, committed, cfg, tags):
    """The phantom fact needs an explicit zero sales row yesterday at the phantom lot's branch (03-RESEARCH Pitfall 4)."""
    seed_json = committed[0]
    phantom = cfg["scenarios"]["demo"]["designed_lots"][tags[ST.PHANTOM_TAG]]
    rows = [r for r in seed_json["daily_sales"]
            if r["branch"] == phantom["branch"] and r["sku"] == phantom["sku"] and r["day"] == ST.TODAY - 1]
    assert len(rows) == 1 and rows[0]["recorded"] == 0
    assert report["lat_phrao_milk_recorded_day2"] == 0


def test_a_seed_json_the_reference_files_do_not_generate_is_refused(tmp_path, committed):
    seed_json, truth, forecast_json = committed
    stale = copy.deepcopy(seed_json)
    stale["dataset"]["input_version"] = "in-000000000000"
    for name, payload in (("seed.json", stale), ("truth.json", truth), ("forecast.json", forecast_json)):
        (tmp_path / name).write_text(S.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="regenerate seed.json"):
        ST.run(tmp_path)
