"""Deterministic test doubles. Simulated call counts are never live-model proof."""
from copy import deepcopy


def _step(ctx):
    from .runner import StepResult
    expected = ctx.references.get("expected", {})
    output = deepcopy(expected.get("reading_fields", {"kind": "NOT_RELEVANT"}))
    if ctx.scenario["id"] == "s10":
        output["contains_instructions"] = True
    if ctx.step["id"] == "brief":
        output = {"draft": deepcopy(expected.get("fact"))}
    if ctx.step["id"] == "reading1":
        output = dict(shelf_units=None, backroom_units=None, photo_observation="A milk slot is visible.",
                      request_status="Photo received · count missing")
    if ctx.step["id"] == "reading2":
        output = dict(typed_counts=deepcopy(expected["typed_counts"]), reply_counts_state="pending manager check",
                      conflict=True)
    usage = {"input": 0, "output": 0, "thoughts": 0}
    if ctx.budget is not None:
        ctx.budget.allow(input_tokens=0, output_tokens=0)
        ctx.budget.charge(usage, calls=1)
    return StepResult(served="live", model_calls=1, cache_hits=0, raw_text="A person reviews the evidence.",
                      displayed_text="A person reviews the evidence.", usage=usage, outputs=output,
                      numbers_removed=0, note=ctx.scenario["id"] == "s10", test_double=True)


DRY_STEPS = dict.fromkeys(("brief", "ask", "reading", "answer_reading", "summary"), _step)
