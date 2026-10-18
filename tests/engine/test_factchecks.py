from dataclasses import replace
import inspect
import json
import math

import pytest

from engine import factchecks
from engine.adjust import InputError
from engine.adjust import effective
from engine.factchecks import fact_checks
from engine.factchecks import _record_plans
from engine.limits import candidates
from engine.model import Lot
from engine.model import TRANSFER, Plan
from engine.model import MARKDOWN
from engine.scoring import score
from engine.scoring import cents
from engine.search import evaluate
from engine.simulate import simulate
from tests.engine.fixtures import BANG_NA, FRI, LAT_PHRAO, R02, SAT, THU, _flat, f1, f6, with_facts
from tests.engine.fixtures import with_overrides
from engine.limits import check_plan
from engine.adjust import AdjustmentError

TOL = 0.01


def checks(inp):
    return fact_checks(inp, evaluate(inp, with_crossovers=False))


def test_f6_fact_check_prices_the_phantom():
    [c] = checks(f6())  # phantom takes precedence over the stale count on the same lot
    assert (c["kind"], c["lot_id"], c["branch"], c["recorded_value"], c["alt_value"]) == \
        ("PHANTOM_STOCK", "MLK-038", "Lat Phrao", 60.0, 0.0)
    assert c["regret"] == pytest.approx(840, abs=TOL)
    assert c["unserved_units"] == pytest.approx(60, abs=TOL)
    assert c["best_plan_if_alt"] == "tr:Lat Phrao:R-02:140:hold"
    assert c["best_plan_if_alt_label"] == "Transfer 140 to Lat Phrao (R-02), hold rest"
    assert c["changes_plan"] is True


def test_unserved_counts_only_demand_the_better_plan_would_serve():
    """Lat Phrao wants 40 + 50 + 80 = 170 but R-02 has room for 140. If its 60 recorded units aren't there, the
    record's plan (transfer 110) leaves 60 unserved and the best plan (transfer 140) leaves 30: acting on the record
    leaves 30 units unserved that a better plan would have served."""
    inp = replace(f6(), runs=(replace(R02, spare_totes=14),),
                  forecasts=tuple(_flat(BANG_NA, {THU: 100, FRI: 100, SAT: 100}) + _flat(LAT_PHRAO, {THU: 40, FRI: 50, SAT: 80})))
    [c] = checks(inp)
    assert evaluate(inp, with_crossovers=False)["recommended"] == "tr:Lat Phrao:R-02:110:md20"
    assert c["best_plan_if_alt"] == "tr:Lat Phrao:R-02:140:hold" and c["unserved_units"] == pytest.approx(30, abs=TOL)


def test_f6_after_confirmed_count_equals_f1():
    confirmed = with_facts(f6(), {"kind": "stock_count", "lot_id": "MLK-038", "units": 0.0})
    result = evaluate(confirmed)
    assert result["recommended"] == "tr:Lat Phrao:R-02:140:hold"
    row = next(r for r in result["options"] if r["key"] == result["recommended"])
    assert row["net"]["base"] == pytest.approx(16_880, abs=TOL)
    assert checks(confirmed) == []  # a lot counted today is no longer suspect


def test_no_rules_fire_on_clean_data():
    assert checks(f1()) == []


def test_estimated_expiry_tests_one_day_earlier():
    inp = f1()
    est = replace(inp, lots=(replace(inp.lots[0], expiry_source="ESTIMATED"),))
    [c] = checks(est)
    assert (c["kind"], c["recorded_value"], c["alt_value"]) == ("ESTIMATED_EXPIRY", 5, 4)


def test_stale_count_tests_seventy_percent():
    inp = f1()
    stale = replace(inp, lots=(replace(inp.lots[0], last_verified_day=THU - 8),))
    [c] = checks(stale)
    assert (c["kind"], c["alt_value"]) == ("STALE_COUNT", 350.0)


def test_a_lot_never_verified_counts_as_stale():
    inp = f1()
    never = replace(inp, lots=(replace(inp.lots[0], last_verified_day=None),))
    assert [c["kind"] for c in checks(never)] == ["STALE_COUNT"]


def test_a_tiny_regret_does_not_count_as_changing_the_plan():
    inp = replace(f6(), policy=replace(f6().policy, min_regret=1_000.0))  # F6's phantom is worth 840
    [c] = checks(inp)
    assert (c["changes_plan"], c["regret"], c["unserved_units"]) == (False, 0.0, 0.0)


def test_a_regret_above_the_policy_minimum_changes_the_plan():
    [c] = checks(replace(f6(), policy=replace(f6().policy, min_regret=800.0)))
    assert (c["changes_plan"], c["regret"]) == (True, pytest.approx(840, abs=TOL))


def test_estimated_expiry_on_a_lot_expiring_today_tests_yesterday_as_all_waste():
    inp = f1()
    today = replace(inp, lots=(replace(inp.lots[0], expiry_day=THU, expiry_source="ESTIMATED"),))
    [c] = checks(today)
    assert (c["recorded_value"], c["alt_value"]) == (THU, THU - 1)
    assert c["best_plan_if_alt"] == "keep"  # already expired under the alternative: nothing to sell or move


def test_count_seven_days_old_is_not_stale():
    inp = f1()
    fresh = replace(inp, lots=(replace(inp.lots[0], last_verified_day=THU - 7),))
    assert checks(fresh) == []


def test_fact_that_does_not_change_the_plan_has_zero_regret():
    inp = f1()
    stale = replace(inp, lots=(replace(inp.lots[0], qty=1000.0, last_verified_day=THU - 8),))
    [c] = checks(stale)  # 700 units would still leave a surplus, so the plan is the same
    assert (c["changes_plan"], c["regret"], c["unserved_units"]) == (False, 0.0, 0.0)


def test_at_most_two_checks_highest_regret_first(monkeypatch):
    inp = f6()
    bn = replace(inp.lots[0], expiry_source="ESTIMATED", last_verified_day=THU - 8)
    many = replace(inp, lots=(bn, inp.lots[1]))
    monkeypatch.setattr(factchecks, "MAX_CHECKS", 10)
    every = checks(many)
    monkeypatch.undo()
    kept = checks(many)
    assert len(every) == 3 and len(kept) == 2
    assert kept == every[:2]
    assert every == sorted(every, key=lambda c: (c["pick_feasible_if_alt"], -c["regret"], c["fact_id"]))  # D-32 order


def test_phantom_needs_zero_sales_yesterday():
    inp = f6()
    sold = replace(inp, sales_yesterday=((BANG_NA, 100.0), ("Lat Phrao", 5.0)))
    assert all(c["kind"] != "PHANTOM_STOCK" for c in checks(sold))


def test_fact_checks_reject_an_input_from_another_engine_version():
    result = evaluate(f6(), with_crossovers=False)
    with pytest.raises(InputError, match="engine_version"):
        fact_checks(replace(f6(), engine_version="engine-0"), result)


@pytest.mark.parametrize("units", [60.0, 20.0])
def test_a_count_confirmed_today_clears_the_phantom(units):
    """D-12: a manager's count at this snapshot sets last_verified_day = today, so MLK-038 is no longer suspect,
    whether the stock is all there (60) or partly there (20). Before the fix these gave PHANTOM 840 and 520."""
    assert checks(with_facts(f6(), {"kind": "stock_count", "lot_id": "MLK-038", "units": units})) == []


def test_a_count_from_yesterday_with_zero_sales_raises_the_phantom_again():
    """D-12: a count from yesterday followed by zero sales yesterday is suspect again; regret is F6's 840."""
    inp = f6()
    counted = replace(inp, lots=(inp.lots[0], replace(inp.lots[1], last_verified_day=THU - 1)))
    [c] = checks(counted)
    assert (c["kind"], c["branch"]) == ("PHANTOM_STOCK", "Lat Phrao")
    assert c["regret"] == pytest.approx(840, abs=TOL)


def test_a_missing_sales_row_is_not_zero_sales():
    """WR-06: with no Lat Phrao row in sales_yesterday its sales are unknown, not zero, so MLK-038 is not phantom.
    It was counted 8 days ago (> 7), so it is still stale."""
    inp = replace(f6(), sales_yesterday=((BANG_NA, 100.0),))
    assert [c["kind"] for c in checks(inp)] == ["STALE_COUNT"]


def _f6_with_a_second_lat_phrao_lot():
    return replace(f6(), lots=f6().lots + (Lot("MLK-039", LAT_PHRAO, 40.0, SAT, received_seq=2, last_verified_day=THU - 8),))


def test_phantom_is_one_fact_per_branch_and_zeroes_every_lot_there():
    """WR-07: one PHANTOM_STOCK fact for Lat Phrao covering MLK-038 (60) and MLK-039 (40), both set to 0.
    - On record Lat Phrao holds 60 + 40 = 100 against 40 + 50 + 50 = 140 demand, so 140 - 100 = 40 (Sat) is unmet
      and the record's plan is Transfer 40 + md20.
    - With both lots at 0 the input is F1: Transfer 140 + hold, 16,880 = 300 x 40 + 140 x 40 - 600 - 60 x 2.
    - The record's plan under the alternative: Bang Na sells 460 at 20% off at 100 x 43/30 a day for Thu-Sat, so
      430 x 32 = 13,760 with 30 wasted x 2 = 60; Lat Phrao sells the 40 arriving Thu against Thu's 40 demand,
      40 x 40 = 1,600. Net 13,760 + 1,600 - 600 - 60 = 14,700, so regret = 16,880 - 14,700 = 2,180.
    - Unserved: Lat Phrao loses Fri 50 + Sat 50 = 100 under the record's plan and 0 under Transfer 140."""
    inp = _f6_with_a_second_lat_phrao_lot()
    assert evaluate(inp, with_crossovers=False)["recommended"] == "tr:Lat Phrao:R-02:40:md20"
    [c] = checks(inp)
    assert (c["fact_id"], c["kind"], c["branch"], c["lot_id"], c["lot_ids"], c["recorded_value"], c["alt_value"]) == \
        ("Lat Phrao:phantom", "PHANTOM_STOCK", "Lat Phrao", "MLK-038", ["MLK-038", "MLK-039"], 100.0, 0.0)
    assert c["best_plan_if_alt"] == "tr:Lat Phrao:R-02:140:hold"
    assert c["regret"] == pytest.approx(2_180, abs=TOL)
    assert c["unserved_units"] == pytest.approx(100, abs=TOL)


def test_a_lot_counted_today_leaves_the_branch_fact_to_the_other_lots():
    """D-12 with WR-07: MLK-038 was counted today (60), so the Lat Phrao fact covers MLK-039 only (recorded 40).
    - With MLK-039 at 0 the input is F6, whose best plan is Transfer 80 + md20 at 18,440.
    - The record's plan (Transfer 40 + md20) earns 13,760 (Bang Na, as above) + 100 x 40 (Lat Phrao sells
      60 + 40 = 100 of its 140 demand) - 600 - 60 = 17,100, so regret = 18,440 - 17,100 = 1,340.
    - Unserved: 140 - 100 = 40 (Sat) under the record's plan, 0 under Transfer 80 (60 + 80 = 140)."""
    inp = with_facts(_f6_with_a_second_lat_phrao_lot(), {"kind": "stock_count", "lot_id": "MLK-038", "units": 60.0})
    [c] = checks(inp)
    assert (c["fact_id"], c["lot_ids"], c["recorded_value"]) == ("Lat Phrao:phantom", ["MLK-039"], 40.0)
    assert c["best_plan_if_alt"] == "tr:Lat Phrao:R-02:80:md20"
    assert c["regret"] == pytest.approx(1_340, abs=TOL)
    assert c["unserved_units"] == pytest.approx(40, abs=TOL)


def test_a_count_still_in_effect_on_a_later_day_does_not_clear_the_phantom():
    """D-18 WR-02: F6 on Fri (R-02 on Fri) with Thu's count of 60 still in effect and zero Thu sales.
    Fri and Sat only; Bang Na 500 and Lat Phrao 60 both expire Sat; demand Bang Na 100 a day, Lat Phrao 50 a day.
    - On the record: Lat Phrao is short only Sat 50 - 10 = 40, so the cap is 40. Transfer q + md20: Bang Na sells
      143.33 a day on 500 - q, so 286.67 x 32 = 9,173.33 and wastes 213.33 - q; Lat Phrao sells 60 + q.
      Net = 9,173.33 - 2(213.33 - q) + 40(60 + q) - 600 = 10,546.67 + 42q, so q = 40 gives 12,226.67. The rest are
      lower: md30 rest 12,080; hold 10,880; md20 alone 11,146.67; md30 11,000; Keep 9,800.
    - With MLK-038 at 0 the cap is 100: Transfer 100 + md20 = 9,173.33 - 2 x 113.33 + 4,000 - 600 = 12,346.67 is
      the best (md30 rest 12,200; hold 11,000).
    - The record's plan if MLK-038 is 0: 9,173.33 - 346.67 + 40 x 40 - 600 = 9,826.67. Regret = 12,346.67 - 9,826.67
      = 2,520.
    - Unserved: Lat Phrao loses Fri 10 + Sat 50 = 60 under the record's plan and 0 under Transfer 100.
    - MLK-038's last count is Thu, not Fri; sales are an explicit 0 and the base forecast today is 50 >= 10, so it is
      phantom. The same count taken at this snapshot (no counted_day) still clears it (D-12)."""
    inp = replace(f6(), today=FRI, runs=(replace(R02, day=FRI),))
    count = {"kind": "stock_count", "lot_id": "MLK-038", "units": 60.0}
    x = with_facts(inp, {**count, "counted_day": THU})
    assert evaluate(x, with_crossovers=False)["recommended"] == "tr:Lat Phrao:R-02:40:md20"
    [c] = checks(x)
    assert (c["kind"], c["branch"], c["lot_ids"], c["recorded_value"]) == ("PHANTOM_STOCK", "Lat Phrao", ["MLK-038"], 60.0)
    assert c["regret"] == pytest.approx(2_520, abs=TOL)
    assert c["unserved_units"] == pytest.approx(60, abs=TOL)
    assert c["best_plan_if_alt"] == "tr:Lat Phrao:R-02:100:md20"
    assert checks(with_facts(inp, count)) == []


def _f6_on_fri_with_two_counts():
    fri = replace(f6(), today=FRI, runs=(replace(R02, day=FRI),))
    count = {"kind": "stock_count", "lot_id": "MLK-038", "units": 60.0}
    return fri, {**count, "counted_day": FRI}, {**count, "counted_day": THU}


def _mlk038(inp):
    lot = next(l for l in effective(inp).lots if l.lot_id == "MLK-038")
    return lot.qty, lot.last_verified_day


def test_two_counts_give_the_same_answer_in_either_order():
    """Round-3 WR-01: F6 on Fri (R-02 on Fri) with a Fri count of 60 and a Thu count of 60 for MLK-038.
    - The Fri count is taken at this snapshot, so it sets the quantity (60) and the day (Fri).
    - The Thu count is from an earlier day, so it sets only the day, and a count never makes the verification
      older: max(Thu, Fri) = Fri. Applied first it sets Thu (later than the record's Thu - 8); the Fri count then
      sets Fri. Either order ends at (60.0, Fri).
    - So MLK-038 is counted at this snapshot and is neither phantom nor stale (D-12). MLK-042 was verified Thu,
      1 day ago (not stale, 1 <= 7), and Bang Na's sales yesterday are 100, not 0. No fact check fires.
    - The two orders still hash to different rec_versions, because fact order is hashed: the fix makes the
      results equal, not the versions."""
    fri, fri_count, thu_count = _f6_on_fri_with_two_counts()
    a = with_facts(fri, fri_count, thu_count)
    b = with_facts(fri, thu_count, fri_count)
    for x in (a, b):
        assert _mlk038(x) == (60.0, FRI)
        assert checks(x) == []
    assert evaluate(a) == evaluate(b)


def test_a_count_never_makes_a_lots_verification_older():
    """Round-3 WR-01: F6 on Fri with MLK-038's record already verified Fri (qty 60), plus a Thu count of 60.
    The Thu count is from an earlier day, so it sets only the day, and max(Thu, Fri) = Fri: the lot stays
    (60.0, Fri), counted at this snapshot, so neither phantom nor stale, and no fact check fires."""
    fri, _, thu_count = _f6_on_fri_with_two_counts()
    verified = replace(fri, lots=tuple(replace(l, last_verified_day=FRI) if l.lot_id == "MLK-038" else l
                                       for l in fri.lots))
    x = with_facts(verified, thu_count)
    assert _mlk038(x) == (60.0, FRI)
    assert checks(x) == []


def test_unserved_units_count_only_the_facts_branch():
    """Spec rev 6.2.3 re-check (ENG-05): unserved_units counts lost demand only at the fact's branch.
    The alternative is F6 with MLK-038 at 0. The record's plan is Transfer 80 to Lat Phrao (R-02), rest marked
    down 20%. Under the alternative:
    - Lat Phrao (demand Thu 40, Fri 50, Sat 50) has only the 80 moved units, arriving Thu: it sells Thu 40 and
      Fri 40, so it loses Fri 10 + Sat 50 = 60.
    - Bang Na's 420 at 20% sells 100 x 43/30 = 143.33 a day, so after Thu and Fri 420 - 286.67 = 133.33 is left.
      On Sat the 20% tier runs f = 133.33 / 143.33 = 0.930 of the day, and the remaining 0.0698 x 100 = 6.98
      has no full-price stock: another branch also loses demand.
    - The best plan under the alternative, Transfer 140 hold, loses nothing at either branch (Lat Phrao gets
      140 = 40 + 50 + 50; Bang Na sells 300 of its 360 at full price).
    So unserved_units = 60 - 0 = 60, the Lat Phrao shortfall only. Without the branch filter the check would
    report 66.98 - 0 = 66.98."""
    inp = f6()
    record_plan = Plan(TRANSFER, 20, 80, LAT_PHRAO, "R-02")
    assert evaluate(inp, with_crossovers=False)["recommended"] == record_plan.key
    alt = effective(replace(inp, lots=tuple(replace(l, qty=0.0) if l.lot_id == "MLK-038" else l
                                            for l in inp.lots)))
    sim = simulate(alt, record_plan, "base")
    lat_phrao = {day: v for (b, day), v in sim.lost_by.items() if b == LAT_PHRAO}
    assert sum(lat_phrao.values()) == pytest.approx(60, abs=TOL)
    assert (lat_phrao.get(THU, 0.0), lat_phrao[FRI], lat_phrao[SAT]) == \
        (pytest.approx(0, abs=TOL), pytest.approx(10, abs=TOL), pytest.approx(50, abs=TOL))
    assert sim.lost_by[(BANG_NA, SAT)] == pytest.approx(6.98, abs=TOL)
    [c] = checks(f6())
    assert c["unserved_units"] == pytest.approx(60, abs=TOL)


def test_stale_and_phantom_thresholds_come_from_the_policy():
    """Round-3 IN-03: the fact checks read their thresholds from the input's policy. Lat Phrao's base forecast
    Thu is 40 < 41, so there is no phantom. MLK-038 was last verified Thu - 8, and 8 > 7, so it is stale, with
    alt 60 x 0.7 = 42 (x 0.5 = 30). With stale_after_days 8, 8 > 8 is false, so there is no fact. MLK-042 was
    counted today. The engine-2 constants still import from engine.factchecks with their values (D-20)."""
    assert (factchecks.STALE_AFTER_DAYS, factchecks.STALE_FACTOR, factchecks.PHANTOM_MIN_FORECAST) == (7, 0.7, 10.0)
    pol = lambda **kw: replace(f6(), policy=replace(f6().policy, **kw))  # noqa: E731
    [c] = checks(pol(phantom_min_forecast=41.0))
    assert (c["kind"], c["fact_id"], c["alt_value"]) == ("STALE_COUNT", "MLK-038:stale", 42.0)
    assert checks(pol(phantom_min_forecast=41.0, stale_factor=0.5))[0]["alt_value"] == 30.0
    assert checks(pol(phantom_min_forecast=41.0, stale_after_days=8)) == []


def _estimated_at(price: float, **policy):
    """F1 with the case lot's expiry ESTIMATED, at a waste price (and any other policy fields)."""
    inp = f1()
    return replace(inp, lots=(replace(inp.lots[0], expiry_source="ESTIMATED"),),
                   policy=replace(inp.policy, waste_price=price, **policy))


def test_decision_regret_uses_the_score_and_reports_its_parts():
    """Spec rev 6.3.0 §6 Fact checks step 3 (ENG-10, D-06). F1 with an ESTIMATED expiry at a waste price of 10.08.
    The record's pick p0 = Transfer 140 md20. Under the alternative (expiry Fri):
    - p0: Bang Na sells 286.67 at 32 = 9,173.33 and wastes 73.33; Lat Phrao sells 90 = 3,600 and wastes 50;
      net 9,173.33 + 3,600 - 600 - 246.67 = 11,926.67; 123.33 units = 127.03 kg; score 10,646.17.
    - p1 = Transfer 90 md30: 320 at 28 = 8,960, Lat Phrao 3,600, waste 90 units; net 11,780; 92.7 kg;
      score 10,845.58. It beats Transfer 90 md20 (score 10,646.19).
    So the decision regret is 10,845.58 - 10,646.17 = 199.41 = -146.67 (net revenue) + 346.08 (waste policy,
    10.08 x 34.33 kg less waste). The UI sentence follows the pattern "Decision regret $7: net revenue -$5,
    waste policy +$12": the waste-policy part is a policy value, not cash."""
    [c] = checks(_estimated_at(10.08))
    assert c["kind"] == "ESTIMATED_EXPIRY"
    assert c["best_plan_if_alt"] == "tr:Lat Phrao:R-02:90:md30"
    assert c["regret"] == pytest.approx(199.41, abs=TOL)
    assert c["net_revenue_difference"] == pytest.approx(-146.67, abs=TOL)
    assert c["waste_policy_difference"] == pytest.approx(346.08, abs=TOL)
    assert c["waste_kg_difference"] == pytest.approx(34.33, abs=TOL)
    assert c["changes_plan"] is True
    assert c["regret"] == round(c["net_revenue_difference"] + c["waste_policy_difference"], 2)  # exact (WR-03)


def test_f6_at_a_waste_price_regret_follows_the_score_best_plan():
    """F6 at a waste price of 10.08 (ENG-10, D-06). The record's pick p0 = Transfer 80 md20 has net 16,040 and 0 kg
    wasted under the alternative (MLK-038 at 0). The score-best p1 = Transfer 140 md20 has net 16,520 and 0 kg, so
    the decision regret is 16,520 - 16,040 = 480, all of it net revenue. A net-based regret with the net-best p1
    (Transfer 140 hold, 16,880) would give 840.
    p1's 360 at Bang Na at 20% sells 143.33 a day, which leaves 73.33 for Sat: f = 73.33 / 143.33 = 0.512, and
    48.84 is lost there. That loss is at another branch, so the unserved units stay Lat Phrao's 60."""
    inp = replace(f6(), policy=replace(f6().policy, waste_price=10.08))
    [c] = checks(inp)
    assert c["regret"] == pytest.approx(480, abs=TOL)
    assert c["best_plan_if_alt"] == "tr:Lat Phrao:R-02:140:md20"
    assert c["net_revenue_difference"] == pytest.approx(480)
    assert c["waste_policy_difference"] == 0.0
    assert c["waste_kg_difference"] == 0.0
    assert c["unserved_units"] == pytest.approx(60, abs=TOL)
    alt = effective(replace(inp, lots=tuple(replace(l, qty=0.0) if l.lot_id == "MLK-038" else l
                                            for l in inp.lots)))
    assert simulate(alt, Plan(TRANSFER, 20, 140, LAT_PHRAO, "R-02"), "base").lost_by[(BANG_NA, SAT)] == \
        pytest.approx(48.84, abs=TOL)


def test_min_regret_does_not_gate_a_count_when_the_pick_breaks_a_limit():
    """02-CONTEXT D-29 (review CR-02), with the values the user approved before execution (02-CONTEXT D-32).
    Renamed and rewritten from test_min_regret_gates_changes_plan_and_the_regret_but_not_the_trade_off (02-18,
    itself renamed from 02-08's test_min_regret_applies_to_the_score_regret_and_zeroes_every_part). Its old values
    at min_regret 200 (changes_plan False, regret 0.0, unserved 0.0) were CR-02 itself: this case's alternative makes
    the recommended transfer break a hard limit, and engine-3 requested no count for it.

    The ESTIMATED case at 10.08 (derivation in test_decision_regret_uses_the_score_and_reports_its_parts): the
    record's pick p0 = Transfer 140 md20; under the alternative (expiry Fri) p0 nets 11,926.67 and wastes 127.03 kg,
    and p1 = Transfer 90 md30 nets 11,780 and wastes 92.7 kg. So the pick changes, and the decision regret is
    10,845.58 - 10,646.17 = 199.41 = -146.67 (net revenue) + 346.08 (waste policy, 10.08 x 34.33 kg less waste; a
    policy value, not cash).

    The demand cap under the alternative: the lot ends Fri, so Lat Phrao (no stock, demand Thu 40, Fri 50, Sat 50)
    has unmet demand through Fri of 40 + 50 = 90 < 140, and check_plan refuses Transfer 140 md20 with exactly
    "no unmet demand at destination": pick_feasible_if_alt is False. D-29 makes a pick that breaks a hard limit
    request a count whatever min_regret, so at 200 and at 199: changes_plan True, regret == decision_regret ==
    199.41, unserved_units 0.0 (Bang Na's demand through Fri is met under both plans). The four money and kg figures
    and the best plan are unchanged. The gate on an allowed pick is still covered by
    test_below_the_minimum_the_pick_change_is_reported_with_its_true_figures (F6 at min_regret 1,000)."""
    plan = Plan(TRANSFER, 20, 140, LAT_PHRAO, "R-02")
    for min_regret in (200.0, 199.0):
        inp = _estimated_at(10.08, min_regret=min_regret)
        assert evaluate(inp, with_crossovers=False)["recommended"] == plan.key
        assert check_plan(with_facts(inp, {"kind": "expiry", "lot_id": "MLK-042", "expiry_day": FRI}), plan) == \
            ["no unmet demand at destination"]
        [c] = checks(inp)
        assert (c["pick_changed"], c["pick_feasible_if_alt"], c["changes_plan"]) == (True, False, True)
        assert c["regret"] == c["decision_regret"]
        assert c["decision_regret"] == pytest.approx(199.41, abs=TOL)
        assert c["unserved_units"] == 0.0
        assert c["net_revenue_difference"] == pytest.approx(-146.67, abs=TOL)
        assert c["waste_policy_difference"] == pytest.approx(346.08, abs=TOL)
        assert c["waste_kg_difference"] == pytest.approx(34.33, abs=TOL)
        assert c["best_plan_if_alt"] == "tr:Lat Phrao:R-02:90:md30"


def test_below_the_minimum_the_pick_change_is_reported_with_its_true_figures():
    """02-CONTEXT D-23: F6 (waste price 0) with min_regret 1,000. The fact is the Lat Phrao phantom (MLK-038 at 0).
    - p0, the record's pick, is Transfer 80 md20: under the alternative it nets 16,040 and wastes 0.
    - p1 is Transfer 140 hold: it nets 16,880 and wastes 60 units x 1.03 = 61.8 kg.
    So the pick changes, and the decision regret is 16,880 - 16,040 = 840, all of it net revenue: at a waste price of
    0 the waste-policy part is 0 (a positive zero). waste_kg_difference = kg(p0) - kg(p1) = 0 - 61.8 = -61.8.
    840 < 1,000, so changes_plan is False and the gated regret and unserved units are 0, while pick_changed is True."""
    [c] = checks(replace(f6(), policy=replace(f6().policy, min_regret=1_000.0)))
    assert (c["pick_changed"], c["changes_plan"], c["regret"], c["unserved_units"]) == (True, False, 0.0, 0.0)
    assert c["decision_regret"] == pytest.approx(840, abs=TOL)
    assert c["net_revenue_difference"] == pytest.approx(840, abs=TOL)
    assert c["waste_policy_difference"] == 0.0
    assert math.copysign(1.0, c["waste_policy_difference"]) == 1.0
    assert c["waste_kg_difference"] == pytest.approx(-61.8, abs=TOL)
    assert c["best_plan_if_alt"] == "tr:Lat Phrao:R-02:140:hold"


def _d23_cases():
    """(input, waste price, check) for every fact check of the D-23 invariant inputs."""
    f1_ = f1()
    same_plan = replace(f1_, lots=(replace(f1_.lots[0], qty=1000.0, last_verified_day=THU - 8),))
    inputs = [
        f1_, f6(), replace(f6(), policy=replace(f6().policy, min_regret=1_000.0)),
        _estimated_at(0.0), _estimated_at(10.08), _estimated_at(10.08, min_regret=200.0),
        same_plan,
        replace(f6(), policy=replace(f6().policy, waste_price=10.08), markdown_schedule=((1, 20), (0, 30))),
        _estimated_at(21.1),
        _cr02_fri(), _cr02_thu(min_regret=0.0),  # review IN-07: the two CR-02 inputs, where p0 is not allowed
    ]
    return [(inp.policy.waste_price, c) for inp in inputs for c in checks(inp)]


_D23_INVARIANTS = {
    "decision_regret_is_the_sum_of_its_parts_or_0":
        lambda price, c: c["decision_regret"] == max(0.0, round(c["net_revenue_difference"]
                                                               + c["waste_policy_difference"], 2)),
    "regret_is_decision_regret_when_the_plan_changes":
        lambda price, c: c["regret"] == (c["decision_regret"] if c["changes_plan"] else 0.0),
    "changes_plan_implies_pick_changed":
        lambda price, c: c["pick_changed"] or not c["changes_plan"],
    "no_pick_change_means_zero_figures":
        lambda price, c: c["pick_changed"] or all(
            c[k] == 0.0 for k in ("regret", "decision_regret", "net_revenue_difference", "waste_policy_difference",
                                  "waste_kg_difference")),
    "at_price_0_decision_regret_is_the_net_difference_or_0":
        lambda price, c: price != 0 or c["decision_regret"] == max(0.0, c["net_revenue_difference"]),
    "an_infeasible_pick_requests_a_count":
        lambda price, c: c["pick_feasible_if_alt"] or c["changes_plan"],
}


@pytest.mark.parametrize("invariant", list(_D23_INVARIANTS))
def test_every_fact_check_keeps_the_d23_invariants(invariant):
    """02-CONTEXT D-23 output contract with D-29 (review IN-07): six invariants, each checked on every fact check of
    eleven inputs (F1; F6; F6 with min_regret 1,000; the ESTIMATED case at 0, at 10.08 and at 10.08 with min_regret
    200; the same-plan stale case; F6 at 10.08 with schedule ((1, 20), (0, 30)); the ESTIMATED case at 21.1; and the
    two CR-02 inputs, the Fri-run case and the Thu-run case at min_regret 0, where the fact makes the record's pick
    break a hard limit):
    - decision_regret == max(0.0, round(net_revenue_difference + waste_policy_difference, 2)), so WR-03's "adds up
      to its displayed parts" holds whenever the score regret is >= 0 (D-29);
    - regret == (decision_regret if changes_plan else 0.0);
    - changes_plan implies pick_changed;
    - with pick_changed False, the five money and kg figures are all 0.0;
    - at a waste price of 0, decision_regret == max(0.0, net_revenue_difference);
    - not pick_feasible_if_alt implies changes_plan: a pick that breaks a hard limit requests a count (D-29).
    Every check also has decision_regret >= 0 and regret >= 0. The D-23 set had no input where p0 is not allowed
    under the alternative, which is how CR-02 got past it (IN-07). The inputs are not vacuous: some check changes the
    plan, some changes the pick below the gate, some keeps the pick, some at a waste price of 0 changes the pick, and
    some has pick_feasible_if_alt False."""
    cases = _d23_cases()
    holds = _D23_INVARIANTS[invariant]
    for price, c in cases:
        assert holds(price, c), (invariant, c["fact_id"], c)
        assert c["decision_regret"] >= 0 and c["regret"] >= 0, (c["fact_id"], c)
    assert any(c["changes_plan"] for _, c in cases)
    assert any(c["pick_changed"] and not c["changes_plan"] for _, c in cases)
    assert any(not c["pick_changed"] for _, c in cases)
    assert any(price == 0 and c["pick_changed"] for price, c in cases)
    assert any(not c["pick_feasible_if_alt"] for _, c in cases)


def test_at_waste_price_zero_the_regret_is_the_net_revenue_difference():
    """F6 at a waste price of 0 (engine-2): p0 = Transfer 80 md20 wastes 0 under the alternative, and p1 = Transfer
    140 hold wastes 60 units = 61.8 kg. p1 wastes more, so the kg difference is negative; at a price of 0 the
    waste-policy part is 0 (a positive zero, never "-0") and the regret equals the net revenue difference, 840."""
    [c] = checks(f6())
    assert c["regret"] == pytest.approx(840)
    assert c["net_revenue_difference"] == pytest.approx(840)
    assert c["waste_policy_difference"] == 0.0
    assert math.copysign(1.0, c["waste_policy_difference"]) == 1.0
    assert c["waste_kg_difference"] == pytest.approx(-61.8, abs=TOL)


def _spare_totes_14():
    """The F6 input of test_unserved_counts_only_demand_the_better_plan_would_serve (R-02 with 14 spare totes)."""
    return replace(f6(), runs=(replace(R02, spare_totes=14),),
                   forecasts=tuple(_flat(BANG_NA, {THU: 100, FRI: 100, SAT: 100}) + _flat(LAT_PHRAO, {THU: 40, FRI: 50, SAT: 80})))


def test_fact_checks_take_p0_and_the_best_transfer_from_the_result():
    """ENG-09 speedup (spec rev 6.3.0 §6 Fact checks, budget): the record's plan p0 and the best transfer are read
    from the case result instead of a second base search. On eight inputs they equal the pair a full re-search
    finds: the plan with the recommended key, and the first scored plan that is a transfer (None when no transfer
    passes, as with no cold chain)."""
    inputs = {
        "f1": f1(),
        "f6": f6(),
        "f6_spare_totes_14": _spare_totes_14(),
        "f1_estimated_10.08": _estimated_at(10.08),
        "f6_10.08": replace(f6(), policy=replace(f6().policy, waste_price=10.08)),
        "f1_schedule_a": replace(f1(), markdown_schedule=((2, 20), (1, 30))),
        "f1_no_cold_chain": with_overrides(f1(), {"kind": "cold_chain", "run_code": "R-02", "value": False}),
        "f1_shortlist_10": replace(f1(), policy=replace(f1().policy, shortlist_step=10)),
    }
    for name, inp in inputs.items():
        result = evaluate(inp, with_crossovers=False)
        eff = effective(inp)
        scored = score(eff, candidates(eff)[0])
        slow_p0 = next(s.plan for s in scored if s.plan.key == result["recommended"])
        slow_best = next((s.plan for s in scored if s.plan.kind == TRANSFER), None)
        assert _record_plans(eff, result) == (slow_p0, slow_best), name
        if name == "f1_no_cold_chain":
            assert slow_best is None


def test_fact_checks_refuse_a_result_that_is_not_this_inputs():
    """Review WR-02: the result must be the one evaluate returned for this same input. F6's own result gives the
    Lat Phrao phantom fact (regret 840); a what-if's result used to give [] silently (the no-cold-chain result has
    no transfer row, so the best transfer was None and Lat Phrao fell out of scope). Now each mismatch is an
    InputError: a what-if's result (also one, like a new quote, that leaves the candidate count unchanged), a
    result from another engine version, a recommended or transfer key that is not a candidate, and a non-dict."""
    own = evaluate(f6(), with_crossovers=False)
    [c] = fact_checks(f6(), own)
    assert (c["fact_id"], c["regret"]) == ("Lat Phrao:phantom", pytest.approx(840, abs=TOL))
    no_cold = with_overrides(f6(), {"kind": "cold_chain", "run_code": "R-02", "value": False})
    new_quote = with_overrides(f6(), {"kind": "transport_quote", "run_code": "R-02", "fixed_cost": 900.0})
    for other in (no_cold, new_quote):
        with pytest.raises(InputError, match="effective_version"):
            fact_checks(f6(), evaluate(other, with_crossovers=False))
    with pytest.raises(InputError, match="engine_version"):
        fact_checks(f6(), {**own, "engine_version": "engine-2"})
    with pytest.raises(InputError, match="not a candidate"):
        fact_checks(f6(), {**own, "recommended": "tr:Rangsit:R-09:10:hold"})
    rows = [{**o, "key": "tr:Rangsit:R-09:10:hold"} if o["kind"] == TRANSFER else o for o in own["options"]]
    with pytest.raises(InputError, match="not a candidate"):
        fact_checks(f6(), {**own, "options": rows})
    with pytest.raises(InputError, match="result dict"):
        fact_checks(f6(), None)


def test_a_result_for_an_equal_effective_input_is_accepted():
    """Review WR-02: effective_version hashes the effective input, so inputs that differ only in what the engine
    never sees share it. F1 on Fri (R-02 on Fri) with a confirmed Thu-only capacity fact covers no day
    (max(3, 4) = 4 > 3, D-16): its result is F1-on-Fri's, and either input's fact checks accept it."""
    fri = replace(f1(), today=FRI, runs=(replace(R02, day=FRI),))
    x = with_facts(fri, {"kind": "capacity", "branch": LAT_PHRAO, "day_from": THU, "day_to": THU, "free_totes": 0})
    r = evaluate(fri, with_crossovers=False)
    assert evaluate(x, with_crossovers=False)["effective_version"] == r["effective_version"]
    assert fact_checks(x, r) == fact_checks(fri, r)


def test_the_regret_is_exactly_the_sum_of_its_reported_parts():
    """Review WR-03: the parts are rounded to cents and the regret is their rounded sum, so the sentence "Decision
    regret $R: net revenue $N, waste policy $P" adds up to the cent whatever the unrounded values. The inputs mix
    waste prices and schedules; before the fix F6 at 1.0 with schedule ((1, 20), (0, 30)) reported a regret 0.01
    below net + policy. At a waste price of 0 the policy part is 0 and the regret is the net revenue difference."""
    cases = [_estimated_at(10.08), _estimated_at(21.1), f6()]
    for price in (0.37, 1.0, 3.33, 10.08):
        cases.append(replace(f6(), policy=replace(f6().policy, waste_price=price),
                             markdown_schedule=((1, 20), (0, 30))))
    seen = 0
    for inp in cases:
        for c in checks(inp):
            if c["changes_plan"]:
                seen += 1
                assert c["regret"] == round(c["net_revenue_difference"] + c["waste_policy_difference"], 2)
    assert seen == len(cases)


def test_signed_figures_never_serialize_as_minus_zero():
    """Review IN-01: a signed figure rounded to cents can be -0.0 (round(-0.001, 2) is -0.0), which json.dumps writes
    as "-0.0" and a naive formatter shows as "-$0.00". scoring.cents rounds to cents and turns a negative zero into
    0.0: cents(-0.001) and cents(-0.004) are positive zeros, cents(-0.006) is -0.01, and cents(2.5) is 2.5.
    Every option row's net, score (low, base, high), delta_vs_keep and shortfall_filled, and every fact check's
    regret, decision_regret, net_revenue_difference, waste_policy_difference and waste_kg_difference, is nonzero or
    a positive zero, for F1 and F6 at waste prices 0 and 10.08, with no schedule and with schedule A."""
    assert json.dumps(cents(-0.001)) == "0.0"
    assert math.copysign(1.0, cents(-0.004)) == 1.0
    assert cents(-0.006) == -0.01
    assert cents(2.5) == 2.5
    signed_zero_free = lambda x: x != 0 or math.copysign(1.0, x) == 1.0  # noqa: E731
    seen = 0
    for base in (f1(), f6()):
        for price in (0.0, 10.08):
            for schedule in ((), ((2, 20), (1, 30))):
                inp = replace(base, policy=replace(base.policy, waste_price=price), markdown_schedule=schedule)
                result = evaluate(inp, with_crossovers=False)
                for row in result["options"]:
                    figures = [*row["net"].values(), *row["score"].values(), row["delta_vs_keep"],
                               row["shortfall_filled"]]
                    assert all(signed_zero_free(x) for x in figures), (row["key"], figures)
                    seen += 1
                for c in fact_checks(inp, result):
                    figures = [c[k] for k in ("regret", "decision_regret", "net_revenue_difference",
                                              "waste_policy_difference", "waste_kg_difference")]
                    assert all(signed_zero_free(x) for x in figures), (c["fact_id"], figures)
                    seen += 1
    assert seen > 8


def test_each_fact_costs_one_search_and_one_simulation(monkeypatch):
    """Review IN-03 (spec rev 6.3.0 §6 Fact checks, budget): each fact costs one search plus one simulation. The
    search under the alternative already holds the best plan's base simulation (score builds each Scored.sims["base"]
    with simulate(alt, plan, "base")), so the only simulation fact_checks runs itself is the record's plan under
    the alternative. Hand count: F6 has one fact (the Lat Phrao phantom), so 1 call; the three-fact input of
    test_at_most_two_checks_highest_regret_first (Bang Na ESTIMATED and stale, plus the phantom) has 3, so 3 calls.
    Before the fix p1 was simulated again: 2 and 6. scoring.score has its own binding of simulate, so the searches'
    simulations are not counted. Since D-29 each fact also runs one limit check of the record's plan under the
    alternative (check_plan, for pick_feasible_if_alt); for a transfer it simulates Keep at the destination through
    engine.limits' own binding of simulate, so it is not counted here either. F6's check is still the phantom at
    regret 840 with 60 unserved units."""
    calls = []
    real = factchecks.simulate

    def counting(*args, **kwargs):
        calls.append(args[1:])
        return real(*args, **kwargs)

    def count(inp):
        result = evaluate(inp, with_crossovers=False)
        calls.clear()
        monkeypatch.setattr(factchecks, "simulate", counting)
        out = fact_checks(inp, result)
        monkeypatch.setattr(factchecks, "simulate", real)
        return len(calls), out

    n, [c] = count(f6())
    assert n == 1
    assert (c["fact_id"], c["regret"], c["unserved_units"]) == \
        ("Lat Phrao:phantom", pytest.approx(840, abs=TOL), pytest.approx(60, abs=TOL))
    inp = f6()
    many = replace(inp, lots=(replace(inp.lots[0], expiry_source="ESTIMATED", last_verified_day=THU - 8), inp.lots[1]))
    assert len(factchecks.suspect_facts(effective(many), *_record_plans(effective(many),
                                                                         evaluate(many, with_crossovers=False)))) == 3
    assert count(many)[0] == 3


def _cr02_fri():
    """F1 with the case lot ESTIMATED, R-02 on Fri and min_sellable_days 2 (review CR-02, the Fri-run case)."""
    return replace(_estimated_at(0.0, min_sellable_days=2), runs=(replace(R02, day=FRI),))


def _cr02_thu(**policy):
    """F1 with the case lot ESTIMATED and min_sellable_days 3, R-02 on Thu (review CR-02, the Thu-run case)."""
    return _estimated_at(0.0, min_sellable_days=3, **policy)


_EXPIRY_FRI = {"kind": "expiry", "lot_id": "MLK-042", "expiry_day": FRI}


def test_a_fact_that_makes_the_pick_break_a_limit_requests_a_count():
    """02-CONTEXT D-29, review CR-02 (the Fri-run case). F1: Bang Na 500 units expiring Sat, base demand 100 a day,
    base m20 = 43/30 (143.33 a day) and m30 = 1.6 (160 a day); price 40 (32 at 20%, 28 at 30%); disposal 2; 1.03 kg a
    unit. Lat Phrao has no stock and demand Thu 40, Fri 50, Sat 50. Here R-02 runs Fri (arrives 13:20, sellable that
    day), quote 600; min_sellable_days 2; waste price 0.
    - Record (expiry Sat): sellable days Sat - Fri + 1 = 2 >= 2. Lat Phrao is short Fri 50 + Sat 50 = 100, so the cap
      is 100. Transfer q with the rest at 20%, 70 <= q <= 100: Bang Na sells all 500 - q at 32, so
      32(500 - q) + 40q - 600 = 15,400 + 8q = 16,200 at q = 100 (below 70, 13,020 + 42q <= 15,960). The rest held
      gives 15,200, the rest at 30% 14,600, Markdown 20% 13,620, Keep 11,600. So p0 = Transfer 100 md20.
    - Alternative (expiry Fri): sellable days Fri - Fri + 1 = 1 < 2, so every transfer is refused; check_plan gives
      exactly ["not enough sellable days at destination"]: pick_feasible_if_alt False. p1 = Markdown 20%:
      9,173.33 - 426.67 = 8,746.67 (213.33 wasted), against Keep 7,400 and Markdown 30% 8,600.
    - p0 simulated anyway: Bang Na's 400 at 20% sells 286.67 = 9,173.33 and wastes 113.33 (226.67); Lat Phrao sells 50
      of the 100 on Fri = 2,000 and wastes 50 (100); 9,173.33 - 226.67 + 2,000 - 100 - 600 = 10,246.67, 163.33 wasted.
    - So net_revenue_difference = 8,746.67 - 10,246.67 = -1,500.0, waste_kg_difference = (163.33 - 213.33) x 1.03 =
      -51.5, waste_policy_difference 0.0, decision_regret = max(0, -1,500) = 0.0. The pick breaks a hard limit, so
      changes_plan is True (D-29) and regret = 0.0; unserved_units 0.0 (Bang Na's demand through Fri is met under both).
    Before the fix (118804f): changes_plan False, decision_regret -1500.0. Engine-2 (4267adb): regret 0.0, changes_plan
    False. D-29 is the approved exception: a fact that makes the recommended plan break a hard limit always requests a
    count, and no regret is negative."""
    inp = _cr02_fri()
    assert evaluate(inp, with_crossovers=False)["recommended"] == "tr:Lat Phrao:R-02:100:md20"
    assert check_plan(with_facts(inp, _EXPIRY_FRI), Plan(TRANSFER, 20, 100, LAT_PHRAO, "R-02")) == \
        ["not enough sellable days at destination"]
    [c] = checks(inp)
    assert c["kind"] == "ESTIMATED_EXPIRY"
    assert (c["pick_changed"], c["pick_feasible_if_alt"], c["changes_plan"]) == (True, False, True)
    assert c["regret"] == 0.0
    assert c["decision_regret"] == 0.0
    assert c["unserved_units"] == 0.0
    assert c["net_revenue_difference"] == pytest.approx(-1500.0, abs=TOL)
    assert c["waste_policy_difference"] == 0.0
    assert c["waste_kg_difference"] == pytest.approx(-51.5, abs=TOL)
    assert c["best_plan_if_alt"] == "md20"


def test_the_regret_is_never_negative_whatever_min_regret():
    """02-CONTEXT D-29, review CR-02 (the Thu-run case). F1 with the case lot ESTIMATED and min_sellable_days 3.
    - Record: R-02 Thu, Sat - Thu + 1 = 3 >= 3, so p0 is F1's pick, Transfer 140 hold (16,880).
    - Alternative (expiry Fri): Fri - Thu + 1 = 2 < 3, so every transfer is refused; check_plan gives exactly
      ["not enough sellable days at destination"]: pick_feasible_if_alt False. p1 = Markdown 20% (8,746.67, 213.33
      wasted).
    - p0 simulated anyway: Bang Na's 360 at full price sells 200 = 8,000 and wastes 160 (320); Lat Phrao gets 140 on
      Thu and sells 40 + 50 = 90 = 3,600, wasting 50 (100); less 600: 10,580, 210 wasted.
    - So net_revenue_difference = 8,746.67 - 10,580 = -1,833.33, waste_kg_difference = (210 - 213.33) x 1.03 = -3.43,
      decision_regret = max(0, -1,833.33) = 0.0, and changes_plan is True (the pick breaks a hard limit), so regret 0.0.
    At min_regret 0, 118804f gave regret -1833.33 and engine-2 (4267adb) gives 0.0; at the default 1.0, 118804f gave
    changes_plan False."""
    plan = Plan(TRANSFER, 0, 140, LAT_PHRAO, "R-02")
    for inp in (_cr02_thu(min_regret=0.0), _cr02_thu()):
        assert evaluate(inp, with_crossovers=False)["recommended"] == "tr:Lat Phrao:R-02:140:hold"
        assert check_plan(with_facts(inp, _EXPIRY_FRI), plan) == ["not enough sellable days at destination"]
        [c] = checks(inp)
        assert (c["pick_changed"], c["pick_feasible_if_alt"], c["changes_plan"]) == (True, False, True)
        assert c["regret"] == 0.0
        assert c["decision_regret"] == 0.0
        assert c["net_revenue_difference"] == pytest.approx(-1833.33, abs=TOL)
        assert c["waste_kg_difference"] == pytest.approx(-3.43, abs=TOL)
        assert c["best_plan_if_alt"] == "md20"


def test_fact_checks_on_a_what_if_input_check_the_pick_without_refusing():
    """02-CONTEXT D-29 on a what-if input. Phase 4's workflow.whatif runs fact_checks on an input carrying what-if
    overrides. check_plan refuses such an input by design: its refusal guards send, approve and prepare, which must
    take the active version's raw input (D-18 WR-01), and that refusal is unchanged. Inside fact_checks the
    alternative is the effective input with the fact applied and nothing left to apply, so only that internal copy
    has its applied_overrides marker cleared before the limit check.
    The override decide_at 540 is F1's own decision minute (09:00), so the effective input is the Fri-run case's:
    the same effective_version, and the same fact check (pick_feasible_if_alt False)."""
    inp = _cr02_fri()
    x = with_overrides(inp, {"kind": "decide_at", "minute": 540})
    assert evaluate(x, with_crossovers=False)["effective_version"] == \
        evaluate(inp, with_crossovers=False)["effective_version"]
    got = fact_checks(x, evaluate(x, with_crossovers=False))
    assert got == checks(inp)
    assert got[0]["pick_feasible_if_alt"] is False
    with pytest.raises(AdjustmentError, match="what-if"):
        check_plan(x, Plan(TRANSFER, 20, 100, LAT_PHRAO, "R-02"))


def two_not_allowed():
    """F6 with the case lot MLK-042 ESTIMATED and last verified Wed, explicit zero sales at both branches and
    min_sellable_days 3 (02-CONTEXT D-32, the two-safety case). Two of its three facts make F6's pick not allowed."""
    base = f6()
    return replace(base, lots=(replace(base.lots[0], expiry_source="ESTIMATED", last_verified_day=THU - 1), base.lots[1]),
                   sales_yesterday=((BANG_NA, 0.0), (LAT_PHRAO, 0.0)),
                   policy=replace(base.policy, min_sellable_days=3))


def test_not_allowed_checks_come_first_then_regret_then_fact_id(monkeypatch):
    """02-CONTEXT D-32 order: (pick_feasible_if_alt, -regret, fact_id), so checks whose alternative makes the
    recommended plan not allowed come first, then the highest regret, then fact_id.
    - Record: p0 = F6's pick, Transfer 80 with the rest at 20% (18,440). The changes reach only the fact rules and the
      sellable-days limit, which R-02 Thu still meets (Sat - Thu + 1 = 3 >= 3).
    - Facts: Bang Na:phantom (explicit 0 sales, base forecast 100 >= 10, MLK-042 verified Wed so uncounted and not
      stale) sets MLK-042 to 0; Lat Phrao:phantom is F6's own fact (MLK-038 to 0); MLK-042:expiry sets the expiry to
      Fri. No stale fact: each lot is covered by a phantom.
    - Bang Na:phantom, not allowed: the stock cap is 0 < 80, so check_plan gives exactly ["no stock to move"]. p1 = Keep
      (no stock at Bang Na, so every transfer is refused and every other plan sells only Lat Phrao's 60 at 40 = 2,400;
      the tie goes to Keep by move rank). p0 under the fact ships nothing, and trip_cost charges R-02's cancel cost,
      None, so the full quote of 600: 2,400 - 600 = 1,800. net +600.0, waste policy 0.0, kg 0.0, decision_regret
      600.0, changes_plan True, regret 600.0, unserved 0.0 (Bang Na's demand is lost under both), best plan "keep".
    - MLK-042:expiry, not allowed: Fri - Thu + 1 = 2 < 3, so check_plan gives exactly ["not enough sellable days at
      destination"]. p1 = Markdown 20%, 9,173.33 - 426.67 + 2,400 = 11,146.67 (213.33 wasted), against Keep 9,800 and
      Markdown 30% 11,000. p0 under the fact: Bang Na's 420 at 20% sell 286.67 for 9,173.33 and waste 133.33 (266.67);
      Lat Phrao's 140 (its 60 expiring Sat plus the 80 expiring Fri, first by FEFO) meet Thu 40 + Fri 50 + Sat 50 for
      5,600; less 600: 13,906.67. net -2,760.0, kg (133.33 - 213.33) x 1.03 = -82.4, decision_regret 0.0,
      changes_plan True, regret 0.0, unserved 0.0, best plan "md20".
    - Lat Phrao:phantom, allowed (Lat Phrao's unmet demand rises to 140 >= 80): F6's check, regret 840.0.
    Engine-2's key (-regret, fact_id) gives Lat Phrao:phantom 840, Bang Na:phantom 600, MLK-042:expiry 0, and its top 2
    drop the expiry safety check. D-32 gives Bang Na:phantom, MLK-042:expiry, Lat Phrao:phantom; the two returned
    are the two not-allowed checks."""
    inp = two_not_allowed()
    plan = Plan(TRANSFER, 20, 80, LAT_PHRAO, "R-02")
    assert evaluate(inp, with_crossovers=False)["recommended"] == plan.key == "tr:Lat Phrao:R-02:80:md20"
    assert check_plan(with_facts(inp, {"kind": "stock_count", "lot_id": "MLK-042", "units": 0.0}), plan) == \
        ["no stock to move"]
    assert check_plan(with_facts(inp, {"kind": "expiry", "lot_id": "MLK-042", "expiry_day": FRI}), plan) == \
        ["not enough sellable days at destination"]
    kept = checks(inp)
    assert [c["fact_id"] for c in kept] == ["Bang Na:phantom", "MLK-042:expiry"]
    bn, ex = kept
    assert (bn["pick_feasible_if_alt"], ex["pick_feasible_if_alt"]) == (False, False)
    assert (bn["regret"], ex["regret"]) == (pytest.approx(600.0, abs=TOL), 0.0)
    assert (bn["changes_plan"], bn["decision_regret"], bn["unserved_units"]) == (True, pytest.approx(600.0, abs=TOL), 0.0)
    assert bn["net_revenue_difference"] == pytest.approx(600.0, abs=TOL)
    assert (bn["waste_policy_difference"], bn["waste_kg_difference"], bn["best_plan_if_alt"]) == (0.0, 0.0, "keep")
    assert (ex["changes_plan"], ex["decision_regret"], ex["unserved_units"], ex["best_plan_if_alt"]) == \
        (True, 0.0, 0.0, "md20")
    assert ex["net_revenue_difference"] == pytest.approx(-2760.0, abs=TOL)
    assert ex["waste_kg_difference"] == pytest.approx(-82.4, abs=TOL)
    monkeypatch.setattr(factchecks, "MAX_CHECKS", 10)
    every = checks(inp)
    assert [c["fact_id"] for c in every] == ["Bang Na:phantom", "MLK-042:expiry", "Lat Phrao:phantom"]
    assert every[:2] == kept
    assert every[2]["pick_feasible_if_alt"] is True
    assert every[2]["regret"] == pytest.approx(840.0, abs=TOL)
    engine2 = sorted(every, key=lambda c: (-c["regret"], c["fact_id"]))
    assert "MLK-042:expiry" not in [c["fact_id"] for c in engine2[:2]]


@pytest.mark.parametrize("cap, extra, expected", [
    (1, 0, ["Bang Na:phantom", "MLK-042:expiry"]),
    (2, 1, ["Bang Na:phantom", "Bang Na:phantom#2", "MLK-042:expiry"]),
    (2, 2, ["Bang Na:phantom", "Bang Na:phantom#2", "Bang Na:phantom#3", "MLK-042:expiry"]),
], ids=["cap_1_two_not_allowed", "three_not_allowed", "four_not_allowed"])
def test_the_cap_never_drops_a_not_allowed_check(monkeypatch, cap, extra, expected):
    """02-CONTEXT D-32 cap: fact_checks returns checks[:max(MAX_CHECKS, the number of not-allowed checks)], so the cap
    never drops a check whose alternative makes the recommended plan not allowed. The list is longer than MAX_CHECKS
    exactly when more checks are not allowed than the cap, and every check past the cap is a not-allowed one.
    On two_not_allowed (derivation in test_not_allowed_checks_come_first_then_regret_then_fact_id):
    - MAX_CHECKS 1, the real rules: two not-allowed checks, so both are returned, 2 > 1.
    - Three and four not-allowed checks at MAX_CHECKS 2: today's three rules raise at most two not-allowed checks (a
      recommended plan breaks a limit only through the case lot's quantity or expiry: transfer_caps reads only the
      case lot and the destination's Keep simulation, a destination fact only lowers recorded stock there, which
      raises unmet demand, and the case lot gets at most one quantity fact and one expiry fact). So the rules
      function is wrapped to append copies of the Bang Na:phantom fact under the fact_ids Bang Na:phantom#2 and #3.
      Each copy has the original's alternative, so the same figures (regret 600.0, "no stock to move", best plan
      "keep"); the copies sort after the original by fact_id, and the expiry check (regret 0) after them. The cap
      rule does not depend on which rule raised a fact.
    In every case Lat Phrao:phantom (allowed, regret 840) is the one check cut."""
    real = factchecks.suspect_facts

    def with_copies(*args, **kwargs):
        facts = real(*args, **kwargs)
        phantom = next(f for f in facts if f["fact_id"] == "Bang Na:phantom")
        return facts + [{**phantom, "fact_id": f"Bang Na:phantom#{k}"} for k in range(2, 2 + extra)]

    monkeypatch.setattr(factchecks, "suspect_facts", with_copies)
    monkeypatch.setattr(factchecks, "MAX_CHECKS", 10)
    every = checks(two_not_allowed())
    monkeypatch.setattr(factchecks, "MAX_CHECKS", cap)
    got = checks(two_not_allowed())
    assert [c["fact_id"] for c in got] == expected
    assert len(got) > factchecks.MAX_CHECKS
    assert all(c["pick_feasible_if_alt"] is False for c in got)
    assert got == [c for c in every if not c["pick_feasible_if_alt"]]
    assert "Lat Phrao:phantom" not in [c["fact_id"] for c in got]
    assert "Lat Phrao:phantom" in [c["fact_id"] for c in every]
    for c in got:
        if c["fact_id"].startswith("Bang Na:phantom"):
            assert (c["regret"], c["decision_regret"], c["changes_plan"], c["best_plan_if_alt"]) == \
                (pytest.approx(600.0, abs=TOL), pytest.approx(600.0, abs=TOL), True, "keep")


def _d35_input(name):
    """02-CONTEXT D-35 inputs: F1 with the case lot's expiry ESTIMATED, waste price 0 unless named.

    Schedules are read from the case lot's expiry D: A = ((2, 20), (1, 30)), the default; B = ((1, 20), (0, 30)).
    """
    A = ((2, 20), (1, 30))
    B = ((1, 20), (0, 30))
    if name == "schedule_b_no_run":
        return replace(_estimated_at(0.0), runs=(), markdown_schedule=B)
    if name == "default_schedule_a_at_10_08":
        return replace(_estimated_at(10.08), runs=(), markdown_schedule=A)
    if name == "schedule_b_transfer_rest":
        return replace(_estimated_at(0.0), runs=(replace(R02, spare_totes=5),), markdown_schedule=B)
    if name == "too_late_run":
        x = _estimated_at(0.0)
        return replace(x, lots=(replace(x.lots[0], last_verified_day=None),), runs=(replace(R02, day=SAT),),
                       markdown_schedule=B)
    raise ValueError(name)


@pytest.mark.parametrize("name,recommended,p0,covered,best", [
    ("schedule_b_no_run", "md20", Plan(MARKDOWN, 20), "the schedule already has it at 20% from day 3", "keep"),
    ("default_schedule_a_at_10_08", "md30", Plan(MARKDOWN, 30), "the schedule already has it at 30% from day 3",
     "keep"),
    ("schedule_b_transfer_rest", "tr:Lat Phrao:R-02:50:md20", Plan(TRANSFER, 20, 50, LAT_PHRAO, "R-02"),
     "the schedule already has it at 20% from day 3", "tr:Lat Phrao:R-02:50:hold"),
], ids=["schedule_b_no_run", "default_schedule_a_at_10_08", "schedule_b_transfer_rest"])
def test_a_markdown_the_schedule_covers_under_the_fact_is_not_not_allowed(name, recommended, p0, covered, best):
    """02-CONTEXT D-35 (refines D-29). F1: Bang Na 500 units expiring Sat (day 5), counted Thu, base demand 100 a day,
    base m20 = 43/30 (143.33 a day) and m30 = 1.6 (160 a day); price 40 (32 at 20%, 28 at 30%); disposal 2; 1.03 kg a
    unit. Lat Phrao has no stock and demand Thu 40, Fri 50, Sat 50. The decision is at 09:00, so a markdown takes
    effect Thu. The case lot is ESTIMATED, so its one fact is MLK-042:expiry (alternative Fri). A lot's price each day
    is the deeper of its plan percent and the schedule's. B, record (D = Sat): Thu 0, Fri 20, Sat 30; alternative
    (D = Fri): Thu 20, Fri 30. A, record: Thu 20, Fri 30, Sat 30; alternative: Thu 30, Fri 30. The covered reason
    names F = D - (the largest k whose step is >= S) = 4 - 1 = 3.

    - schedule_b_no_run (no run, B): record Keep 4,000 + 4,586.67 + 4,480 - 2 x 96.67 = 12,873.33, Markdown 20%
      4,586.67 + 4,586.67 + 4,480 - 2 x 53.33 = 13,546.67, Markdown 30% 13,440 - 40 = 13,400: md20, not covered on
      the record (Thu 0%). Under the alternative Markdown 20% is covered (Thu 20%). p1 = Keep, 4,586.67 + 4,480 -
      2 x 196.67 = 8,673.33, over Markdown 30% (8,600). p0 under the alternative is priced at the deeper of 20 and the
      schedule each day, which is Keep's price: every figure 0.0, best keep.
    - default_schedule_a_at_10_08 (no run, A, waste price 10.08): record Keep net 4,586.67 + 4,480 + 4,480 - 2 x 36.67
      = 13,473.33 wasting 37.77 kg, score 13,473.33 - 380.69 = 13,092.64; Markdown 30% net 13,400 wasting 20.6 kg,
      score 13,192.35; Markdown 20% is covered on the record. So md30. Under the alternative Thu is 30%, so Markdown
      30% is covered; p1 = Keep, the only candidate, at p0's prices: every figure 0.0, best keep.
    - schedule_b_transfer_rest (B, R-02 with 5 spare totes, so the truck cap is 50): record, Lat Phrao receives 50 on
      Thu and sells 40 Thu at 40 and 10 Fri at 32 = 1,920; Bang Na's 450 at 20% net 13,653.33 - 6.67; less 600:
      14,966.67, over the rest held (14,293.33) and at 30% (13,920). So p0 = Transfer 50 md20, not covered on the
      record. Under the alternative the rest at 20% is covered (Thu 20%), while sellable days (2 >= 1), demand (90 >=
      50), storage, truck (50 >= 50) and stock all pass. p1 = Transfer 50 hold: 1,600 + 9,066.67 - 293.33 - 600 =
      9,773.33, over the rest at 30% (9,700) and Keep (8,673.33). p0 under the alternative has the hold's prices:
      every figure 0.0, best tr:Lat Phrao:R-02:50:hold. check_plan returns the same covered text for a transfer's
      rest as for a markdown.

    In each, check_plan under the Fri expiry gives exactly the one covered reason. Before the fix pick_feasible_if_alt
    was False and changes_plan True: a count requested for a step the schedule takes anyway. D-35: a covered markdown is
    a redundancy, not a broken limit; if the fact is true the schedule marks the lot down anyway, though check_plan, and
    so send, approve and prepare, would refuse the manual step, or for a transfer the whole plan, on that record
    (contract-diff row 42, reviews IN-11 and IN-14). So pick_feasible_if_alt is True and the check goes through the
    ordinary min_regret gate: regret 0 < 1.0, so changes_plan False; at min_regret 0.0, 0 >= 0, so changes_plan True
    with regret 0.0.
    """
    inp = _d35_input(name)
    assert evaluate(inp, with_crossovers=False)["recommended"] == recommended
    assert check_plan(with_facts(inp, _EXPIRY_FRI), p0) == [covered]
    [c] = checks(inp)
    assert c["fact_id"] == "MLK-042:expiry"
    assert (c["pick_changed"], c["pick_feasible_if_alt"], c["changes_plan"]) == (True, True, False)
    for key in ("regret", "decision_regret", "unserved_units", "net_revenue_difference", "waste_policy_difference",
                "waste_kg_difference"):
        assert c[key] == pytest.approx(0.0, abs=TOL), key
    assert c["best_plan_if_alt"] == best
    [c0] = checks(replace(inp, policy=replace(inp.policy, min_regret=0.0)))
    assert c0["pick_feasible_if_alt"] is True and c0["changes_plan"] is True
    assert c0["regret"] == pytest.approx(0.0, abs=TOL)


def test_a_run_that_arrives_too_late_under_the_fact_stays_not_allowed_and_first():
    """02-CONTEXT D-35 with D-29 and D-32: the exclusion is narrow. F1 with the case lot ESTIMATED and never verified
    (last_verified_day None, so a stale fact too), R-02 on Sat, schedule B = ((1, 20), (0, 30)), waste price 0.

    - Record (expiry Sat; B gives Thu 0, Fri 20, Sat 30): Bang Na's 450 at 20% give 13,653.33 - 6.67; Lat Phrao sells
      the 50 on Sat at 28 = 1,400; less 600: 14,446.67. The rest held gives 13,773.33 and Markdown 20% 13,546.67, so
      p0 = tr:Lat Phrao:R-02:50:md20.
    - MLK-042:expiry (Fri; B gives Thu 20, Fri 30): check_plan gives exactly ["the schedule already has it at 20% from
      day 3", "not enough sellable days at destination"] (4 - 5 + 1 = 0 < 1). D-35 ignores only the first, so the
      check is still not allowed: pick_feasible_if_alt False, changes_plan True. p1 = Keep 8,673.33. p0 under the
      alternative ships nothing inside the Fri horizon and sells at Keep's prices: regret 0.0, decision_regret 0.0,
      best keep.
    - MLK-042:stale (qty 0.7 x 500 = 350): allowed (stock cap 350 >= 50; the record's expiry, so nothing covered; the
      run keeps its Sat day). p1 = Keep: all 350 sell, Thu 100 at 40, Fri 143.33 at 32, Sat 106.67 at 28: 11,573.33.
      p0: all 350 sell too, Thu and Fri 286.67 at 32 = 9,173.33 and the 63.33 left on Sat at 28 = 1,773.33, less
      600: 10,346.67. regret 1,226.67, changes_plan True, best keep.
    - Order: D-32's key gives [MLK-042:expiry (False, 0.0), MLK-042:stale (True, -1,226.67)]; engine-2's
      (-regret, fact_id) would put MLK-042:stale first.
    A covered reason next to a real broken limit still leaves the pick not allowed (D-29) and first (D-32). This test
    passes before the D-35 fix too: it is a guard on the narrowness of the exclusion, not the RED.
    """
    inp = _d35_input("too_late_run")
    assert evaluate(inp, with_crossovers=False)["recommended"] == "tr:Lat Phrao:R-02:50:md20"
    assert check_plan(with_facts(inp, _EXPIRY_FRI), Plan(TRANSFER, 20, 50, LAT_PHRAO, "R-02")) == \
        ["the schedule already has it at 20% from day 3", "not enough sellable days at destination"]
    cs = checks(inp)
    assert [c["fact_id"] for c in cs] == ["MLK-042:expiry", "MLK-042:stale"]
    e, s = cs
    assert (e["pick_feasible_if_alt"], e["changes_plan"]) == (False, True)
    assert e["regret"] == pytest.approx(0.0, abs=TOL)
    assert e["decision_regret"] == pytest.approx(0.0, abs=TOL)
    assert e["best_plan_if_alt"] == "keep"
    assert (s["pick_feasible_if_alt"], s["changes_plan"]) == (True, True)
    assert s["regret"] == pytest.approx(1226.67, abs=TOL)
    assert s["best_plan_if_alt"] == "keep"
    assert [c["fact_id"] for c in sorted(cs, key=lambda c: (-c["regret"], c["fact_id"]))] == \
        ["MLK-042:stale", "MLK-042:expiry"]


def test_the_fact_check_docs_name_the_float_score_and_the_d32_sort_key():
    """Review IN-08, fixed per 02-CONTEXT D-34: the fact-check wording that D-24 and D-32 made stale. The fact checks
    compare the float Scored.score (net minus waste price x kg), not the ranking's exact_score (review IN-05), and
    since D-32 the sort key is (pick_feasible_if_alt, -regret, fact_id), so regret is only its second element.
    Phases 4 and 5 read this module to word the UI, so the module text, the fact_checks docstring and the comment on
    the regret line say what the code computes.

    Review IN-11 (UAT gap G-02-2): since D-35, pick_feasible_if_alt is also True when check_plan's only reason is the
    covered markdown, so the text says what check_plan, and so send, approve and prepare, would refuse. Review IN-14
    (UAT gap G-02-2b): check_plan gates send, approve and prepare (limits.check_plan's docstring, spec §8 Recheck
    contract), so the wording names all three, in the IN-14 reviewer's words."""
    module_doc = factchecks.__doc__
    assert "on the score (net minus waste price \u00d7 kg, `Scored.score`)" in module_doc
    assert "ranking score" not in module_doc
    fc_doc = fact_checks.__doc__
    assert "the second sort key after pick_feasible_if_alt, D-32" in fc_doc
    assert "engine-2, the sort key" not in fc_doc
    source = inspect.getsource(fact_checks)
    assert "on the ranking score" not in source
    assert "decision regret on the score (net minus waste price \u00d7 kg, Scored.score)" in source
    flat = lambda s: " ".join(s.split())  # noqa: E731
    in11 = ("pick_feasible_if_alt is True when check_plan(alt, p0) gives no reason, or only the covered-markdown "
            "reason for p0's percent (D-35). In that case the schedule already gives the same outcome, though "
            "check_plan, and so send, approve and prepare, would refuse the manual step, or for a transfer "
            "the whole plan, on that record")
    assert in11 in flat(module_doc)
    assert in11 in flat(fc_doc)
    for text in (module_doc, fc_doc, source):
        assert "every hard limit" not in text
    assert "refuses the manual step by name" not in module_doc
    assert "refuses the manual step by name" not in source
