"""Accounting hand cases; no database or simulator fixture is used."""
import importlib.util
import math

import pytest


def metrics_module():
    assert importlib.util.find_spec('api.metrics') is not None, 'record metrics must exist'
    from api import metrics
    return metrics


@pytest.mark.parametrize('net_ss,net_s1,unshipped_ss,unshipped_s1,expected', [
    (1000, 900, 0, 100, 0), (900, 1000, 100, 0, 0),
    (1000, 900, 150, 100, 150), (1000, 950, 0, 0, 50),
    (1000, 950, 0, 0, 50),
])
def test_cancel_cost_hand_cases(net_ss, net_s1, unshipped_ss, unshipped_s1, expected):
    assert metrics_module().footnote_x(net_ss, net_s1, unshipped_ss, unshipped_s1) == expected


@pytest.mark.parametrize('values,expected', [
    ({'store_rule': 900, 'store_rule_checked': 950, 'shelfshift': 1000}, [900, 50, 50, 1000]),
    ({'store_rule': 900, 'store_rule_checked': 850, 'shelfshift': 800}, [900, -50, -50, 800]),
    ({'store_rule': 0, 'store_rule_checked': -0.0, 'shelfshift': 0}, [0, 0, 0, 0]),
])
def test_steps_keep_signed_changes(values, expected):
    result = metrics_module().steps(values)
    assert [s['value'] for s in result] == expected
    assert [s['kind'] for s in result] == ['total', 'change', 'change', 'total']
    assert all(math.copysign(1, s['value']) == 1 for s in result if s['value'] == 0)


@pytest.mark.parametrize('planned,shipped,expected', [(10, 0, 'failed'), (10, 3, 'short'), (10, 10, None)])
def test_trip_class_boundaries(planned, shipped, expected):
    assert metrics_module().trip_class(planned, shipped) == expected


def test_money_rounds_inputs_to_cents_without_negative_zero():
    m = metrics_module()
    assert m.footnote_x(10.015, 10.005, 0, 0) == .01
    assert math.copysign(1, m.cents(-.001)) == 1


def test_money_rows_add_by_world_and_day_including_negative_net():
    m = metrics_module()
    rows = [dict(scenario=w, day=d, branch=b, net=(d - 2) * 10)
            for w in m.WORLD_KEYS for d in range(7) for b in ('a', 'b')]
    result = m.money(rows)
    for world, key in m.WORLD_KEYS.items():
        assert result['by_scenario'][key] == sum(r['net'] for r in rows if r['scenario'] == world)
        assert [d[key] for d in result['by_day']] == [-40, -20, 0, 20, 40, 60, 80]


def test_waste_is_daily_and_reference_is_markdowns_only():
    m = metrics_module()
    rows = [dict(scenario=w, day=d, wasted=i, wasted_kg=i * .2)
            for i, w in enumerate(m.WORLD_KEYS, 1) for d in range(7)]
    result = m.waste(rows)
    assert result['totals']['shelfshift']['units'] == 21
    assert [d['shelfshift'] for d in result['by_day']] == [3] * 7
    assert result['change_vs_markdowns_only']['shelfshift']['units'] == -7
    assert result['change_vs_markdowns_only']['markdowns_only'] == {'units': 0, 'kg': 0}
