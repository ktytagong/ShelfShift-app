"""Alternative fact searches preserve full-score results without unused scenarios."""
from dataclasses import replace
import math

import pytest

from engine import factchecks, scoring
from engine.adjust import InputError, effective
from engine.limits import candidates
from engine.search import evaluate
from tests.engine.fixtures import THU, f1, f6


def test_fact_search_only_simulates_the_base_it_reports(monkeypatch):
    inp = f6()
    result = evaluate(inp, with_crossovers=False)
    scenarios = []
    original = scoring.simulate

    def traced(inp, plan, scenario):
        scenarios.append(scenario)
        return original(inp, plan, scenario)

    monkeypatch.setattr(scoring, "simulate", traced)
    [check] = factchecks.fact_checks(inp, result)
    assert check["regret"] == 840.0
    assert check["unserved_units"] == 60.0
    assert check["best_plan_if_alt"] == "tr:Lat Phrao:R-02:140:hold"
    assert scenarios and set(scenarios) == {"base"}


def parity_cases():
    clean, phantom = f1(), f6()
    stale = replace(clean, lots=(replace(clean.lots[0], last_verified_day=THU - 8),))
    expiry = replace(clean, lots=(replace(clean.lots[0], expiry_source="ESTIMATED"),))
    many = replace(phantom, lots=(replace(phantom.lots[0], expiry_source="ESTIMATED",
                                        last_verified_day=THU - 8), phantom.lots[1]))
    spread = replace(phantom, forecasts=tuple(replace(f, low=f.base * 0.8, high=f.base * 1.2)
                                             for f in phantom.forecasts))
    scheduled = replace(spread, markdown_schedule=((2, 20), (1, 30)),
                        policy=replace(spread.policy, waste_price=10.08))
    return [pytest.param(phantom, id="phantom"), pytest.param(stale, id="stale"),
            pytest.param(expiry, id="expiry"), pytest.param(many, id="ordering-and-cap"),
            pytest.param(spread, id="different-scenarios"), pytest.param(scheduled, id="schedule-and-waste"),
            pytest.param(replace(scheduled, policy=replace(scheduled.policy, shortlist_step=5)), id="shortlist-5"),
            pytest.param(replace(scheduled, policy=replace(scheduled.policy, shortlist_step=10)), id="shortlist-10")]


@pytest.mark.parametrize("inp", parity_cases())
def test_complete_fact_checks_equal_the_three_scenario_search(inp, monkeypatch):
    result = evaluate(inp, with_crossovers=False)
    actual = factchecks.fact_checks(inp, result)
    optimized = scoring.best_base

    def three_scenario_oracle(alt, plans):
        expected = scoring.score(alt, plans)[0]
        got = optimized(alt, plans)
        assert got.plan is expected.plan
        assert got.sims == {"base": expected.sims["base"]}
        assert scoring.rank_key(got) == scoring.rank_key(expected)
        assert (got.net(), got.kg(), got.score()) == (expected.net(), expected.kg(), expected.score())
        return expected

    monkeypatch.setattr(factchecks, "best_base", three_scenario_oracle)
    assert factchecks.fact_checks(inp, result) == actual


def test_base_helper_runs_once_per_candidate_and_full_score_keeps_all_scenarios(monkeypatch):
    inp = effective(f6())
    plans = candidates(inp)[0][:3]
    calls = []
    original = scoring.simulate

    def traced(inp, plan, scenario):
        calls.append((plan, scenario))
        return original(inp, plan, scenario)

    monkeypatch.setattr(scoring, "simulate", traced)
    scoring.best_base(inp, plans)
    assert calls == [(plan, "base") for plan in plans]
    calls.clear()
    rows = scoring.score(inp, plans)
    assert len(calls) == 3 * len(plans)
    assert all(set(row.sims) == {"low", "base", "high"} for row in rows)


@pytest.mark.parametrize("scenario", ["low", "high"])
def test_unused_scenarios_still_require_valid_input(scenario, monkeypatch):
    inp = f6()
    result = evaluate(inp, with_crossovers=False)
    invalid = replace(inp, forecasts=(replace(inp.forecasts[0], **{scenario: math.nan}), *inp.forecasts[1:]))

    def forbidden(*args):
        pytest.fail("invalid forecasts must be refused before the alternative search")

    monkeypatch.setattr(factchecks, "best_base", forbidden)
    with pytest.raises(InputError):
        factchecks.fact_checks(invalid, result)
