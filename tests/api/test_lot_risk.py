"""The at-risk figure is the case lot's own projected waste under Keep (quick 261018-h9t; user decision 2026-10-18).

The board card's `at_risk_units`, `at_risk_kg` and `at_risk_value` come from screen.lot_keep_waste: the morning
screen's Keep projection for the case lot alone (the same figure as `case_flags.keep_waste`). The Keep option's
`wasted_units` in the result stays what it is: the plan's network aggregate over every branch in the engine input.

fixtures/sus12460_input.json is SUS-12460's stored engine input from the public set (product codes only). It is a
regression pin of engine-3 output, not a hand fixture: its expected numbers were read from the engine, not derived
by hand.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from api import cases, db, refdata, screen
from engine import evaluate, from_dict
from tests.api.conftest import fresh

SUS12460 = pathlib.Path(__file__).parent / "fixtures" / "sus12460_input.json"


def test_sus12460_shows_the_lots_18_units_and_83_stays_the_network_aggregate():
    raw = json.loads(SUS12460.read_text())
    assert raw["case_lot_id"] == "S229-P38-2-r"
    lot = screen.lot_keep_waste(raw)
    assert lot == pytest.approx(18.27, abs=0.01)  # the lot's own Keep projection: the at-risk figure
    keep = next(o for o in evaluate(from_dict(raw))["options"] if o["key"] == "keep")
    assert keep["wasted_units"] == pytest.approx(82.74, abs=0.01)  # every branch in the input: a network aggregate
    assert round(lot) == 18 and round(keep["wasted_units"]) == 83


def test_a_case_card_carries_its_lots_figure_not_the_keep_options_network_total(run):
    ref = refdata.get(run["dataset_version"])
    with db.tx() as conn:
        r = fresh(run["run_id"])
        cards = cases.board(conn, r)
        recs = {x["case_id"]: x for x in db.rows(
            conn, "SELECT r.case_id, r.engine_input, r.result FROM cases c JOIN recommendations r ON r.run_id = "
                  "c.run_id AND r.case_id = c.case_id AND r.rec_version = c.active_rec_version WHERE c.run_id = :r",
            r=run["run_id"])}
    differs = 0
    for card in cards:
        rec = recs.get(card["case_id"])
        if rec is None:
            assert (card["at_risk_units"], card["at_risk_kg"], card["at_risk_value"]) == (None, None, None)
            continue
        units = screen.lot_keep_waste(rec["engine_input"])
        product = ref.products[card["sku"]]
        assert card["at_risk_units"] == round(units, 2)
        assert card["at_risk_kg"] == round(units * product.kg_per_unit, 2)
        assert card["at_risk_value"] == round(units * product.price, 2)
        keep = next(o for o in rec["result"]["options"] if o["key"] == "keep")
        differs += abs(keep["wasted_units"] - units) > 1e-6
    assert differs, "no synthetic case has a network Keep total different from its lot's own figure"


def test_cards_map_the_helpers_figure_whatever_the_keep_option_says(run, monkeypatch):
    monkeypatch.setattr(screen, "lot_keep_waste", lambda engine_input: 7.5)
    ref = refdata.get(run["dataset_version"])
    with db.tx() as conn:
        cards = cases.board(conn, fresh(run["run_id"]))
    priced = [c for c in cards if c["rec_version"]]
    assert priced
    for c in priced:
        product = ref.products[c["sku"]]
        assert (c["at_risk_units"], c["at_risk_kg"], c["at_risk_value"]) == (
            7.5, round(7.5 * product.kg_per_unit, 2), round(7.5 * product.price, 2))
