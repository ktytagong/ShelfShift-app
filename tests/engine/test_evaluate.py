from dataclasses import replace

import pytest

from engine.adjust import InputError
from engine.model import ENGINE_VERSION, TRANSFER
from engine.search import evaluate
from engine.simulate import HorizonError
from tests.engine.fixtures import FRI, R02, THU, f1, f6, with_overrides
from tests.engine.fixtures import LAT_PHRAO, SAT, with_facts
from engine.model import Capacity, Forecast
from tests.engine.fixtures import BANG_NA
from engine.limits import check_plan
from engine.model import Plan
from engine.model import MARKDOWN

TOL = 0.01

# A Fri 01:00 loading cutoff with 2 h packing means deciding by Thu 23:00.
OVERNIGHT = replace(R02, day=FRI, loading_cutoff_min=60, departs_min=90, arrives_min=140)


def nets(result):
    return {r["key"]: r["net"]["base"] for r in result["options"]}


def test_f1_recommends_transfer_140_hold():
    result = evaluate(f1())
    assert result["recommended"] == "tr:Lat Phrao:R-02:140:hold"
    assert result["recommended_label"] == "Transfer 140 to Lat Phrao (R-02), hold rest"
    assert result["scenario_wins"]["wins"] == 3
    assert result["crossovers"]["transport"]["value"] == 3_860
    assert nets(result) == pytest.approx({
        "keep": 11_600, "md20": 13_620, "md30": 13_400, "tr:Lat Phrao:R-02:140:hold": 16_880,
        "tr:Lat Phrao:R-02:140:md20": 16_520, "tr:Lat Phrao:R-02:140:md30": 15_080}, abs=TOL)


def test_f1_option_row_fields():
    row = next(r for r in evaluate(f1())["options"] if r["key"] == "tr:Lat Phrao:R-02:140:hold")
    assert row["delta_vs_keep"] == pytest.approx(5_280, abs=TOL)
    assert (row["sold"], row["wasted_units"], row["wasted_kg"]) == (440, 60, pytest.approx(61.8))
    assert row["shortfall_filled"] == pytest.approx(140)


def test_f1_wait_shows_the_latest_decision_time():
    assert evaluate(f1())["wait"]["text"] == \
        "transfer becomes impossible after 10:00 (run R-02 loading cutoff 12:00 minus 2 h packing)"


def test_wait_text_shows_packing_that_is_not_whole_hours():
    inp = replace(f1(), policy=replace(f1().policy, packing_min=90))
    assert evaluate(inp)["wait"]["text"].endswith("minus 90 min packing)")


def test_f2_quote_4500_markdown_wins():
    result = evaluate(with_overrides(f1(), {"kind": "transport_quote", "run_code": "R-02", "fixed_cost": 4_500.0}))
    assert result["recommended"] == "md20"
    assert nets(result)["md20"] == pytest.approx(13_620, abs=TOL)
    assert nets(result)["tr:Lat Phrao:R-02:140:hold"] == pytest.approx(12_980, abs=TOL)
    assert nets(result)["tr:Lat Phrao:R-02:140:md20"] == pytest.approx(12_620, abs=TOL)


def test_f3_cold_chain_unavailable():
    result = evaluate(with_overrides(f1(), {"kind": "cold_chain", "run_code": "R-02", "value": False}))
    assert result["recommended"] == "md20"
    assert result["rejected"] == [{"label": "Transfer to Lat Phrao (R-02)", "dest": "Lat Phrao", "run_code": "R-02",
                                   "reasons": ["cold chain"]}]
    assert result["wait"] is None


def test_f4_decide_at_1030():
    result = evaluate(with_overrides(f1(), {"kind": "decide_at", "minute": 630}))
    assert result["rejected"][0]["reasons"] == ["past decision deadline 10:00"]
    assert all(r["kind"] != TRANSFER for r in result["options"])


def test_f6_on_the_record():
    result = evaluate(f6())
    assert result["recommended"] == "tr:Lat Phrao:R-02:80:md20"
    assert nets(result) == pytest.approx({
        "keep": 14_000, "md20": 16_020, "md30": 15_800, "tr:Lat Phrao:R-02:80:hold": 16_760,
        "tr:Lat Phrao:R-02:80:md20": 18_440, "tr:Lat Phrao:R-02:80:md30": 16_760}, abs=TOL)


def test_scenarios_can_disagree():
    inp = f1()
    # under low demand Lat Phrao wants far less, so a markdown wins there
    fc = tuple(replace(f, low=f.base * 0.2) if f.branch == "Lat Phrao" else f for f in inp.forecasts)
    result = evaluate(replace(inp, forecasts=fc))
    wins = result["scenario_wins"]
    assert wins["wins"] == 2
    assert wins["by_scenario"]["low"] != result["recommended"]


def test_evaluate_is_deterministic():
    assert evaluate(f6()) == evaluate(f6())


def test_missing_destination_forecast_names_branch_and_day():
    inp = f1()
    gap = replace(inp, forecasts=tuple(f for f in inp.forecasts if not (f.branch == "Lat Phrao" and f.day == 5)))
    with pytest.raises(HorizonError, match="Lat Phrao on day 5"):
        evaluate(gap)


def overnight(minute):
    return with_overrides(replace(f1(), runs=(OVERNIGHT,)), {"kind": "decide_at", "minute": minute})


def test_no_transfer_is_recommended_after_the_real_deadline():
    result = evaluate(overnight(1410))  # Thu 23:30, past the Thu 23:00 deadline
    assert result["rejected"] == [{"label": "Transfer to Lat Phrao (R-02)", "dest": "Lat Phrao", "run_code": "R-02",
                                   "reasons": ["past decision deadline 23:00"]}]
    assert all(r["kind"] != TRANSFER for r in result["options"])
    assert result["wait"] is None


def test_wait_gives_the_deadline_day_and_minute_across_midnight():
    assert evaluate(overnight(1350))["wait"] == {
        "run_code": "R-02", "day": THU, "latest_decision_min": 1380,
        "text": "transfer becomes impossible after 23:00 (run R-02 loading cutoff 01:00 minus 2 h packing)"}


def test_wait_names_the_day_when_the_deadline_is_not_today():
    inp = with_overrides(replace(f1(), runs=(replace(R02, day=FRI),)), {"kind": "decide_at", "minute": 630})
    assert evaluate(inp)["wait"]["text"] == \
        "transfer becomes impossible after day 4 10:00 (run R-02 loading cutoff 12:00 minus 2 h packing)"


def test_wait_tie_goes_to_the_lowest_run_code_whatever_the_input_order():
    r03 = replace(R02, run_code="R-03")
    a = replace(f1(), runs=(R02, r03))
    b = replace(f1(), runs=(r03, R02))
    assert evaluate(a)["wait"]["run_code"] == "R-02"
    assert evaluate(a) == evaluate(b)


def test_evaluate_rejects_an_input_from_another_engine_version():
    with pytest.raises(InputError, match="engine_version"):
        evaluate(replace(f1(), engine_version="engine-0"))
    assert evaluate(f1())["engine_version"] == ENGINE_VERSION


def test_evaluate_rejects_a_markdown_pct_the_allocator_cannot_sell():
    inp = f1()
    with pytest.raises(InputError, match="markdown_pct"):
        evaluate(replace(inp, lots=(replace(inp.lots[0], markdown_pct=10),)))


def test_an_ended_range_changes_no_recommendation():
    """D-16, the verifier's repro: F1 on Sat with an adopted Thu-Fri multiplier (covers no day: max(3, 5) = 5 > 4).
    Only Sat is simulated: Bang Na demand 100, lot 500.
    - Keep: 100 x 40 - 400 x 2 = 3,200.
    - md20 (09:00 + 1 h <= 12:00, so it starts the same day): 143.33 x 32 - 356.67 x 2 = 4,586.67 - 713.33 = 3,873.33.
    - md30: 160 x 28 - 340 x 2 = 4,480 - 680 = 3,800.
    - R-02 runs Thu, so its deadline day 3 10:00 is before Sat.
    F1 on Fri (R-02 on Fri) with a confirmed Thu-only capacity fact also evaluates as without it."""
    sat = replace(f1(), today=SAT)
    ended = with_overrides(sat, {"kind": "demand_multiplier", "branch": LAT_PHRAO, "day_from": THU, "day_to": FRI,
                                 "value": 1.5})
    result = evaluate(ended)
    assert result == evaluate(sat)
    assert result["recommended"] == "md20"
    assert nets(result) == pytest.approx({"keep": 3_200, "md20": 3_873.33, "md30": 3_800}, abs=TOL)
    assert result["rejected"][0]["reasons"] == ["past decision deadline day 3 10:00"]
    fri = replace(f1(), today=FRI, runs=(replace(R02, day=FRI),))
    thu = {"kind": "capacity", "branch": LAT_PHRAO, "day_from": THU, "day_to": THU, "free_totes": 0}
    assert evaluate(with_facts(fri, thu)) == evaluate(fri)


def f1_at_waste_price(price):
    return replace(f1(), policy=replace(f1().policy, waste_price=price))


def test_a_waste_price_can_change_the_pick_but_never_a_net():
    """Rev 6.3.0 (ENG-10): plans rank by score = net - waste price x kg wasted; nets are pure dollars.

    F1, kg_per_unit 1.03, waste price 10.08 ($0.28/kg x 36 in fixture money). Hand scores: Keep 11,600 - 2,076.48 =
    9,523.52; md20 13,620 - 726.768 = 12,893.23; md30 13,400 - 207.648 = 13,192.35; hold 140 16,880 - 622.944 =
    16,257.06; md20 140 16,520 (no waste); md30 140 15,080. 16,520 > 16,257.06, so Transfer 140, markdown rest 20%
    ranks first, while every net and the hold row's delta vs keep stay as at a waste price of 0.
    """
    result = evaluate(f1_at_waste_price(10.08))
    assert result["recommended"] == "tr:Lat Phrao:R-02:140:md20"
    assert nets(result) == pytest.approx({
        "keep": 11_600, "md20": 13_620, "md30": 13_400, "tr:Lat Phrao:R-02:140:hold": 16_880,
        "tr:Lat Phrao:R-02:140:md20": 16_520, "tr:Lat Phrao:R-02:140:md30": 15_080}, abs=TOL)
    hold = next(r for r in result["options"] if r["key"] == "tr:Lat Phrao:R-02:140:hold")
    assert hold["delta_vs_keep"] == pytest.approx(5_280, abs=TOL)


def test_option_rows_carry_the_score_and_the_waste_policy_amount():
    """Rev 6.3.0 (ENG-10): every row carries score (low, base, high) and waste_policy (base waste price x kg) beside
    the net. Hand values at 10.08 (kg_per_unit 1.03): hold 140 wastes 60 units = 61.8 kg, waste_policy 622.944,
    score 16,880 - 622.944 = 16,257.06; md20 140 wastes nothing, score 16,520, waste_policy 0; md30 wastes 20 units
    = 20.6 kg, waste_policy 207.648, score 13,400 - 207.648 = 13,192.35. At a waste price of 0 score is net and
    waste_policy is 0 on every row."""
    rows = {r["key"]: r for r in evaluate(f1_at_waste_price(10.08))["options"]}
    hold = rows["tr:Lat Phrao:R-02:140:hold"]
    assert hold["score"]["base"] == pytest.approx(16_257.06, abs=TOL)
    assert hold["waste_policy"] == pytest.approx(622.94, abs=TOL)
    assert hold["net"]["base"] == pytest.approx(16_880, abs=TOL)
    md20_140 = rows["tr:Lat Phrao:R-02:140:md20"]
    assert md20_140["score"]["base"] == pytest.approx(16_520, abs=TOL)
    assert md20_140["waste_policy"] == 0.0
    md30 = rows["md30"]
    assert md30["score"]["base"] == pytest.approx(13_192.35, abs=TOL)
    assert md30["waste_policy"] == pytest.approx(207.65, abs=TOL)
    for row in evaluate(f1())["options"]:
        assert row["score"] == row["net"]
        assert row["waste_policy"] == 0.0


def test_scenario_wins_count_the_score():
    """Rev 6.3.0 (ENG-10): scenario wins rank each scenario by its score. F1's low, base and high are equal, so at
    10.08 each scenario ranks alike and Transfer 140, markdown rest 20% (16,520 > 16,257.06) wins 3 of 3."""
    wins = evaluate(f1_at_waste_price(10.08))["scenario_wins"]
    assert wins["wins"] == 3
    assert set(wins["by_scenario"].values()) == {"tr:Lat Phrao:R-02:140:md20"}


def test_the_waste_flip_is_reported_at_every_price():
    """Rev 6.3.0 (ENG-10, D-03): the flip price is reported with the other crossovers at every price, including 0,
    and absent without them."""
    w = evaluate(f1())["crossovers"]["waste_price"]
    assert w["price"] == 0.0
    assert w["rises_at"]["result"] == "threshold"
    assert "crossovers" not in evaluate(f1(), with_crossovers=False)


def test_the_shortlist_label_names_the_plans_checked():
    """Rev 6.3.0 (ENG-09): with the shortlist on, the result says how many plans it checked, and the F1 and F6
    picks stay; with it off there is no shortlist key."""
    r1 = evaluate(replace(f1(), policy=replace(f1().policy, shortlist_step=10)))
    assert r1["candidates_checked"] == 45
    assert r1["shortlist"] == {"step": 10, "label": "wins among the 45 plans checked"}
    assert r1["recommended"] == "tr:Lat Phrao:R-02:140:hold"
    r6 = evaluate(replace(f6(), policy=replace(f6().policy, shortlist_step=5)))
    assert r6["candidates_checked"] == 51
    assert r6["recommended"] == "tr:Lat Phrao:R-02:80:md20"
    assert "shortlist" not in evaluate(f1())


def test_the_shortlist_is_deterministic_whatever_the_run_order():
    """ENG-09 ordering: with the shortlist on, two runs in either order give equal results; 3 + 2 x 14 x 3 plans."""
    inp = replace(f1(), policy=replace(f1().policy, shortlist_step=10))
    r03 = replace(R02, run_code="R-03")
    forward = evaluate(replace(inp, runs=(R02, r03)))
    assert forward == evaluate(replace(inp, runs=(r03, R02)))
    assert forward["candidates_checked"] == 87


# rev 6.3.0 (ESL-02, D-09, D-10): the next markdown step, the deadline before it and the earlier of the two.
A = ((2, 20), (1, 30))
B = ((1, 20), (0, 30))
C = ((0, 30),)
NO_COLD_CHAIN = {"kind": "cold_chain", "run_code": "R-02", "value": False}


def sched(inp, schedule):
    return replace(inp, markdown_schedule=schedule)


def sunday_lot(**lot):
    """The SUS-19 shape: F1 with the case lot expiring Sun (day 6), forecasts for day 6 at Bang Na (100) and
    Lat Phrao (50), and Lat Phrao capacity 16 on day 6."""
    base = f1()
    return replace(base, lots=(replace(base.lots[0], expiry_day=6, **lot),),
                   forecasts=base.forecasts + (Forecast(BANG_NA, 6, 100, 100, 100), Forecast(LAT_PHRAO, 6, 50, 50, 50)),
                   capacity=base.capacity + (Capacity(LAT_PHRAO, 6, 16),))


def test_markdown_step_names_the_next_deeper_step():
    """Today's step is already the price; steps not deeper are skipped.
    F1 + A on Thu: A is at 20% from Thu (D-2), so the next deeper step is 30% on Fri (D-1).
    Sunday lot + A: 0% today, 20% on Fri (day 4), 30% on Sat (day 5): the next is Fri at 20%.
    The same lot recorded at 20%: Fri's 20% is not deeper, so the next is Sat at 30%."""
    assert evaluate(sched(f1(), A))["markdown_step"] == {"day": 4, "pct": 30, "days_until": 1}
    assert evaluate(sched(sunday_lot(), A))["markdown_step"] == {"day": 4, "pct": 20, "days_until": 1}
    assert evaluate(sched(sunday_lot(markdown_pct=20), A))["markdown_step"] == {"day": 5, "pct": 30, "days_until": 2}


def test_no_schedule_or_no_deeper_step_means_no_markdown_step():
    plain = evaluate(f1())
    assert "markdown_step" not in plain and "act_before" not in plain
    base = f1()
    at_30 = evaluate(sched(replace(base, lots=(replace(base.lots[0], markdown_pct=30),)), A))
    assert "markdown_step" not in at_30 and "act_before" not in at_30


def test_act_before_is_the_end_of_the_day_before_the_step():
    assert evaluate(sched(f1(), A))["act_before"] == {
        "day": 3, "latest_decision_min": 1439,
        "text": "act by 23:59: the schedule takes the lot to 30% off at opening on day 4"}


R02_RUN_PART = {"day": 3, "latest_decision_min": 600,
                "text": "transfer becomes impossible after 10:00 (run R-02 loading cutoff 12:00 minus 2 h packing)",
                "source": "run"}


def test_decide_by_is_the_recommended_plans_own_deadline():
    """D-21 (option A, user 2026-10-13): decide_by is the recommended plan's own deadline, the last minute check_plan
    accepts it. A transfer's run part is its own run's latest decision time (source "run"); a markdown or a transfer's
    remainder under a schedule adds the markdown effective-day cutoff (Task 2); the earlier part wins, a tie goes to
    "run"; absent when nothing limits the plan. This supersedes D-10's "earlier of wait and act_before", so the old
    schedule-B Fri-run and schedule-A-without-cold-chain assertions are gone: under D-21 they depend on which plan is
    recommended, and not every one of those is hand-derivable.

    F1 (Thu, decision 09:00, packing 2 h, R-02 cutoff 12:00, so Thu 10:00 = day 3 minute 600): Transfer 140 hold
    (16,880) is recommended, so decide_by is R-02's run part.
    F1 + A: Transfer 140 hold nets 14,202.67 (02-11 world test), above Keep 13,473.33 and Markdown 30% 13,400
    (Markdown 20% is covered), so some transfer on R-02 wins. Under A only the 30% rest is uncovered; its markdown part
    would be Thu 11:00 (F = 5 - 1 = 4), later than 10:00, so either way decide_by is R-02's run part.
    F6: Transfer 80, rest at 20% (18,440) is recommended; with no schedule the rest has no markdown part, so decide_by
    is the R-02 run part, and check_plan accepts the plan at 10:00 and refuses it at 10:01 (nothing is covered).
    No cold chain: Markdown 20% (13,620) beats Markdown 30% (13,400) and Keep (11,600); with no schedule nothing limits
    a markdown, so decide_by is absent. With A: Keep 143.33 x 32 + 320 x 28 - 2 x 36.67 = 13,473.33 beats Markdown 30%
    480 x 28 - 2 x 20 = 13,400 (Markdown 20% is covered), so decide_by is absent.
    """
    assert evaluate(f1())["decide_by"] == R02_RUN_PART
    with_a = evaluate(sched(f1(), A))
    assert with_a["recommended"].startswith("tr:Lat Phrao:R-02:")
    assert with_a["decide_by"] == R02_RUN_PART
    r6 = evaluate(f6())
    assert r6["recommended"] == "tr:Lat Phrao:R-02:80:md20"
    assert r6["decide_by"] == R02_RUN_PART
    plan = Plan(TRANSFER, 20, 80, LAT_PHRAO, "R-02")
    assert check_plan(replace(f6(), decision_min=600), plan) == []
    assert check_plan(replace(f6(), decision_min=601), plan) == ["past decision deadline 10:00"]
    assert "decide_by" not in evaluate(with_overrides(f1(), NO_COLD_CHAIN))
    assert "decide_by" not in evaluate(sched(with_overrides(f1(), NO_COLD_CHAIN), A))


def test_decide_by_ignores_a_later_run_the_recommended_plan_does_not_use():
    """D-21: decide_by never names a run the recommended plan does not use; wait keeps its engine-2 meaning.

    F1 plus R-03 = R-02 on Fri (day 4) at a 10,000 quote. Any R-03 plan pays 10,000 against at most 500 x 40 = 20,000
    of revenue, so it nets under 16,880, and R-02's Transfer 140 hold still wins. wait is the latest run over every
    scored transfer: R-03, day 4, 10:00. decide_by is R-02's own deadline, Thu 10:00 (source "run"), and check_plan
    accepts the R-02 plan at 10:00 and refuses it at 10:01 with "past decision deadline 10:00".
    """
    r03 = replace(R02, run_code="R-03", day=FRI, fixed_cost=10_000.0)
    inp = replace(f1(), runs=(R02, r03))
    result = evaluate(inp)
    assert result["recommended"] == "tr:Lat Phrao:R-02:140:hold"
    assert result["wait"] == {
        "run_code": "R-03", "day": 4, "latest_decision_min": 600,
        "text": "transfer becomes impossible after day 4 10:00 (run R-03 loading cutoff 12:00 minus 2 h packing)"}
    assert result["decide_by"] == R02_RUN_PART
    plan = Plan(TRANSFER, 0, 140, LAT_PHRAO, "R-02")
    assert check_plan(replace(inp, decision_min=600), plan) == []
    assert check_plan(replace(inp, decision_min=601), plan) == ["past decision deadline 10:00"]


def _policy(inp, **fields):
    return replace(inp, policy=replace(inp.policy, **fields))


def _at(inp, today, minute):
    return replace(inp, today=today, decision_min=minute)


def test_the_markdown_part_of_decide_by_is_the_last_minute_check_plan_accepts_the_plan():
    """D-21 markdown part: option A (user, 2026-10-13). decide_by is the last minute check_plan accepts the
    recommended plan, including the markdown effective-day cutoff. For percent P > 0 under a schedule:
    F = expiry - the largest k among the steps whose pct is at least P (any covering step, not only the next);
    c = markdown_same_day_by_min - relabel_min; the part is day F-1 at min(c, 1439), or day F-2 at 23:59 when c < 0;
    S = the schedule's percent on day F; a markdown plan's text "mark down by <at>: from day F the schedule already
    has it at S%", a transfer's rest "decide by <at>: from day F the schedule already has the rest at S%", because
    after that minute the whole transfer is refused, not only its markdown (review IN-06).
    act_before stays a separate key and is never this part. Fixture policy: 720 - 60 = 660 (11:00); expiry Sat (5).

    no_run (F1 without runs), schedule B = 20% Fri (day 4), 30% Sat (day 5):
    - Markdown 20%: 143.33 Thu at 32, 143.33 Fri at 32, 160 Sat at the schedule's 30% (at 28), 53.33 wasted:
      9,173.33 + 4,480 - 106.67 = 13,546.67. Markdown 30%: 480 at 28, 20 wasted: 13,400. Keep: 100 at 40,
      143.33 at 32, 160 at 28, 96.67 wasted: 13,066.67 - 193.33 = 12,873.33. Markdown 20% wins; F = 5 - 1 = 4, S 20.
    schedule C = 30% Sat: Markdown 20% again 13,546.67 against 13,400 and Keep 8,000 + 4,480 - 280 = 12,200;
      F = 5 - 0 = 5 (the 30% step covers 20%), S 30, part day 4 11:00.
    C with relabel_min 800 (every markdown starts the next day): Markdown 20% from Fri 4,000 + 143.33 x 32 + 160 x 28
      - 2 x 96.67 = 12,873.33, Markdown 30% from Fri 4,000 + 8,960 - 160 = 12,800, Keep 12,200. Markdown 20% wins;
      c = -80, so the part is day 3 23:59.
    B at waste price 10.08 (kg 1.03): Markdown 20% 13,546.67 - 54.93 kg x 10.08 = 12,992.94, Markdown 30% 13,400 -
      20.6 x 10.08 = 13,192.35, Keep 12,873.33 - 99.57 x 10.08 = 11,869.70. Markdown 30% wins. The next step (Fri
      20%) is markdown_step and act_before is Thu 23:59, but that step does not cover 30%; the later Sat step does, so
      F = 5 and the part is Fri 11:00, later than act_before.
    tight = F1, schedule ((1, 20),) (20% Fri and Sat), Lat Phrao storage 7 totes (70 units): Transfer 70, rest at
      20% = 13,760 + 1,600 + 30 x 32 - 600 = 15,720, no waste; Transfer 70 hold 15,046.67; Transfer 70 md30 14,000;
      Transfer q md20 (40 <= q < 70) 13,340 + 34q <= 15,686; Markdown 20% 13,620; Markdown 30% 13,400; Keep
      12,946.67. A 09:00 markdown starts Thu for relabel 60, 120 and 180, so no net depends on it. The rest's F = 4;
      its part is Thu at 720 - relabel: 11:00 (60), 10:00 (120, a tie with the run, which goes to "run"), 09:00
      (180, earlier than the run's 10:00).
    """
    no_run = replace(f1(), runs=())
    md20, md30 = Plan(MARKDOWN, 20), Plan(MARKDOWN, 30)

    with_b = sched(no_run, B)
    result = evaluate(with_b)
    assert result["recommended"] == "md20"
    assert result["decide_by"] == {
        "day": 3, "latest_decision_min": 660,
        "text": "mark down by 11:00: from day 4 the schedule already has it at 20%", "source": "markdown"}
    assert check_plan(_at(with_b, 3, 659), md20) == []
    assert check_plan(_at(with_b, 3, 660), md20) == []
    assert check_plan(_at(with_b, 3, 661), md20) == ["the schedule already has it at 20% from day 4"]

    with_c = sched(no_run, C)
    result = evaluate(with_c)
    assert result["recommended"] == "md20"
    assert result["decide_by"] == {
        "day": 4, "latest_decision_min": 660,
        "text": "mark down by day 4 11:00: from day 5 the schedule already has it at 30%", "source": "markdown"}
    assert check_plan(_at(with_c, 4, 659), md20) == []
    assert check_plan(_at(with_c, 4, 660), md20) == []
    assert check_plan(_at(with_c, 4, 661), md20) == ["the schedule already has it at 30% from day 5"]

    slow = _policy(with_c, relabel_min=800)
    result = evaluate(slow)
    assert result["recommended"] == "md20"
    assert result["decide_by"] == {
        "day": 3, "latest_decision_min": 1439,
        "text": "mark down by 23:59: from day 5 the schedule already has it at 30%", "source": "markdown"}
    assert check_plan(_at(slow, 3, 1438), md20) == []
    assert check_plan(_at(slow, 3, 1439), md20) == []
    assert check_plan(_at(slow, 4, 0), md20) == ["the schedule already has it at 30% from day 5"]

    priced = _policy(with_b, waste_price=10.08)  # a later step, not the next, covers the plan
    result = evaluate(priced)
    assert result["recommended"] == "md30"
    assert result["markdown_step"] == {"day": 4, "pct": 20, "days_until": 1}
    assert (result["act_before"]["day"], result["act_before"]["latest_decision_min"]) == (3, 1439)
    assert result["decide_by"] == {
        "day": 4, "latest_decision_min": 660,
        "text": "mark down by day 4 11:00: from day 5 the schedule already has it at 30%", "source": "markdown"}
    assert check_plan(_at(priced, 4, 659), md30) == []
    assert check_plan(_at(priced, 4, 660), md30) == []
    assert check_plan(_at(priced, 4, 661), md30) == ["the schedule already has it at 30% from day 5"]

    tight = replace(f1(), markdown_schedule=((1, 20),), capacity=tuple(Capacity(LAT_PHRAO, d, 7) for d in (THU, FRI, SAT)))
    plan = Plan(TRANSFER, 20, 70, LAT_PHRAO, "R-02")
    late = ["past decision deadline 10:00"]
    covered = ["the schedule already has it at 20% from day 4"]
    for relabel in (60, 120, 180):
        inp = _policy(tight, relabel_min=relabel)
        result = evaluate(inp)
        assert result["recommended"] == "tr:Lat Phrao:R-02:70:md20"
        assert nets(result)["tr:Lat Phrao:R-02:70:md20"] == pytest.approx(15_720, abs=TOL)
        if relabel in (60, 120):
            assert result["decide_by"] == R02_RUN_PART
        else:
            assert result["decide_by"] == {
                "day": 3, "latest_decision_min": 540,
                "text": "decide by 09:00: from day 4 the schedule already has the rest at 20%", "source": "markdown"}
    at60, at120, at180 = (_policy(tight, relabel_min=r) for r in (60, 120, 180))
    assert check_plan(_at(at60, 3, 599), plan) == []
    assert check_plan(_at(at60, 3, 600), plan) == []
    assert check_plan(_at(at60, 3, 601), plan) == late
    assert check_plan(_at(at120, 3, 600), plan) == []
    assert set(check_plan(_at(at120, 3, 601), plan)) == set(covered + late)
    assert check_plan(_at(at180, 3, 539), plan) == []
    assert check_plan(_at(at180, 3, 540), plan) == []
    assert check_plan(_at(at180, 3, 541), plan) == covered


def test_each_row_score_is_its_displayed_net_minus_its_displayed_waste_policy():
    """Review WR-03: the row's base score is the rounded net minus the rounded waste-policy amount, so the three
    figures on a row add up to the cent (the ranking compares the exact score on 6-place components, D-24). Before
    the fix F1 at 1.0 with schedule ((1, 20), (0, 30)) showed Markdown 20 at net 13,546.67, waste policy 54.93 and
    score 13,491.73, not 13,546.67 - 54.93 = 13,491.74. At a waste price of 0 the score is the net."""
    for price in (0.0, 0.37, 1.0, 3.33, 10.08):
        for schedule in ((), ((2, 20), (1, 30)), ((1, 20), (0, 30))):
            for base in (f1(), f6()):
                inp = replace(base, policy=replace(base.policy, waste_price=price), markdown_schedule=schedule)
                for row in evaluate(inp, with_crossovers=False)["options"]:
                    assert row["score"]["base"] == round(row["net"]["base"] - row["waste_policy"], 2)
                    if price == 0.0:
                        assert row["score"] == row["net"]
