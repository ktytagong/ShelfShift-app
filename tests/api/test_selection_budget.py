"""The selected plan's fact checks against the case budget (spec §4; 07-CONTEXT D-23; CLAUDE.md engine rules).

Selecting another plan computes that option's own fact checks (`engine.fact_checks` with `recommended` replaced: one
search per suspect fact, research F-21), so `selection.select` is timed on the story case for every option other than
the recommended one that check_plan accepts today. Each option is selected three times (the recommended key clears it
in between, untimed, so every timed call computes the checks again) and its median is kept. The times are printed;
the slowest must stay under the spec's 3 s case budget. ENGINE_WORKER is as the suite runs it.
"""
import statistics
import time

from api import selection
from tests.api.conftest import seat
from tests.api.test_selection import active, feasible_others
from tests.api.test_workflow import call, version
from tests.api.verify_helpers import story

CASE_BUDGET_S = 3.0  # spec §4's case budget: the spec's number, never raised or tuned (CLAUDE.md engine rules)
ROUNDS = 3


def test_select_fits_the_case_budget(run):
    """Every feasible non-recommended option's select (its recheck and its own fact checks) fits the case budget."""
    case = story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    v = version(run, cid)
    recommended = active(run, cid)["result"]["recommended"]
    keys = feasible_others(run, cid)
    assert keys, "hand-back: the story case has no other feasible option to time"
    medians = {}
    for key in keys:
        times = []
        for _ in range(ROUNDS):
            start = time.perf_counter()
            out = call(run, selection.select, src, cid, v, key)
            times.append(time.perf_counter() - start)
            assert out["selected"]["plan_key"] == key
            call(run, selection.select, src, cid, v, recommended)  # clears it: the next select computes again
        medians[key] = statistics.median(times)
        print(f"select {key}: median {medians[key]:.3f} s of {', '.join(f'{t:.3f}' for t in times)}")
    slowest = max(medians, key=medians.get)
    print(f"slowest: {slowest} {medians[slowest]:.3f} s (case budget {CASE_BUDGET_S} s)")
    assert medians[slowest] < CASE_BUDGET_S, (slowest, medians)
