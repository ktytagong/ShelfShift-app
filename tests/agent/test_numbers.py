"""09-05 contracts: exact values, unit matching and sentence-level removal.

Production imports are deliberately inside tests: collection precedes agent/.
The optional ref keyword carries the existing reference currency/calendar, not
a second formatting/configuration store. All numbers below are test inputs.
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest


REMOVED = "⚠ number removed: not traceable to a tool result"
TRANSFER = "tr:S241:R-02:82:md20"
RESULTS = {"R1": {
    "options": {"md20": {"net": {"base": 742.64}, "wasted_units": 70.0, "pct": 20},
                TRANSFER: {"net": {"base": 806.0}, "qty": 82}},
    "deadline_min": 600, "expiry_day": 5, "scenario_wins": {"wins": 2, "tested": 3},
    "rows": [{"qty": 82}], "missing": None, "allowed": True,
}}


def checked(text, results=None, *, currency="USD", day0=date(2031, 1, 1)):
    from agent.numbers import check, render
    ref = SimpleNamespace(currency=currency, date=lambda day: day0 + timedelta(days=day))
    out = check(text, RESULTS if results is None else results, ref=ref)
    return render(out["text"], out["refs"]), out


def test_a_reference_becomes_the_trusted_value_with_its_unit():
    text, out = checked("Markdown 20% earns [[R1:options.md20.net.base]] by [[R1:deadline_min]].")
    assert text == "Markdown 20% earns USD 742.64 by 10:00."
    assert (out["claims"], out["removed"]) == (3, 0)
    assert [(r["result_id"], r["path"], r["kind"], r["value"]) for r in out["refs"]] == [
        ("R1", "options.md20.net.base", "money", 742.64), ("R1", "deadline_min", "time", 600.0)]


def test_plan_keys_with_colons_resolve_without_reinterpreting_the_key():
    text, out = checked(f"Transfer earns [[R1:options.{TRANSFER}.net.base]].")
    assert text == "Transfer earns USD 806.00."
    assert out["refs"][0]["path"] == f"options.{TRANSFER}.net.base"
    assert (out["claims"], out["removed"]) == (1, 0)


@pytest.mark.parametrize("path", ["missing", "allowed", "rows", "rows.1.qty", "rows.-1.qty",
                                       "rows.0.qty.extra", "options.md20.net", "__class__", "rows..qty"])
def test_only_existing_numeric_leaves_can_be_referenced(path):
    text, out = checked(f"Value [[R1:{path}]].")
    assert (text, out["refs"], out["claims"], out["removed"]) == (REMOVED, [], 0, 1)


def test_list_leaf_and_equal_values_keep_reference_order():
    text, out = checked("First [[R1:rows.0.qty]], then [[R1:options." + TRANSFER + ".qty]].")
    assert text == "First 82, then 82."
    assert [r["path"] for r in out["refs"]] == ["rows.0.qty", f"options.{TRANSFER}.qty"]
    assert (out["claims"], out["removed"]) == (2, 0)


def test_a_result_not_supplied_in_the_authorized_scope_cannot_resolve():
    text, out = checked("Foreign [[R9:qty]]. Own [[R1:rows.0.qty]].")
    assert text == f"{REMOVED} Own 82."
    assert (out["claims"], out["removed"]) == (1, 1)
    assert [r["result_id"] for r in out["refs"]] == ["R1"]


@pytest.mark.parametrize("sentence,removed", [
    ("Net USD 742.64.", 0), ("Net USD 742.65.", 1), ("Net USD 743.", 0),
    ("Waste USD 70.", 1), ("Waste 70 units.", 0), ("Markdown 20%.", 0),
    ("Markdown 21%.", 1), ("Decide by 10:00.", 0), ("Decide by 10:01.", 1),
])
def test_bare_numbers_must_match_a_tool_result_of_the_same_kind(sentence, removed):
    text, out = checked(sentence)
    assert text == (REMOVED if removed else sentence)
    assert (out["claims"], out["removed"]) == (0 if removed else 1, removed)


@pytest.mark.parametrize("value,removed", [(10.494, 1), (10.495, 0), (10.496, 0), (10.506, 1)])
def test_half_unit_precision_boundary_uses_the_canonical_tolerance(value, removed):
    text, out = checked("Value 10.50.", {"R1": {"qty": value}})
    assert (text, out["claims"], out["removed"]) == (
        REMOVED if removed else "Value 10.50.", 0 if removed else 1, removed)


@pytest.mark.parametrize("text", ["", "  \n ", "The manager must review this case."])
def test_empty_or_nonnumerical_text_has_zero_numerical_claims(text):
    rendered, out = checked(text, {})
    assert rendered == text.strip()
    assert (out["claims"], out["removed"], out["refs"]) == (0, 0, [])


def test_dataset_codes_and_adjacent_units_do_not_inflate_counts():
    text, out = checked("C12 S241 P17 G9 SUS-19 R-02: 82 units; 70 units; 82 units.")
    assert text == "C12 S241 P17 G9 SUS-19 R-02: 82 units; 70 units; 82 units."
    assert (out["claims"], out["removed"]) == (3, 0)


def test_unicode_punctuation_and_sentence_order_survive_one_removed_sentence():
    text, out = checked("Keep the record. It saves 9999 units. S241’s lot has [[R1:rows.0.qty]] units.")
    assert text == f"Keep the record. {REMOVED} S241’s lot has 82 units."
    assert (out["claims"], out["removed"], len(out["refs"])) == (1, 1, 1)


def test_missing_source_is_not_a_zero_and_a_failed_sentence_loses_all_its_refs():
    text, out = checked("Zero [[R1:missing]] and [[R1:rows.0.qty]]. Still 82 units.")
    assert text == f"{REMOVED} Still 82 units."
    assert (out["claims"], out["removed"], out["refs"]) == (1, 1, [])


def test_money_and_dates_follow_the_supplied_reference_not_the_real_clock():
    text, out = checked("Net [[R1:options.md20.net.base]]. Expires [[R1:expiry_day]].", currency="EUR")
    assert text == "Net EUR 742.64. Expires Mon 6 Jan."
    assert (out["claims"], out["removed"]) == (2, 0)
    assert checked("Expires 6 Jan.", currency="EUR")[1]["removed"] == 0
    assert checked("Expires 7 Jan.", currency="EUR")[0] == REMOVED


def test_traceable_observation_does_not_manufacture_a_safety_or_reasoning_badge():
    text, out = checked("Reported 82 units. A manager must confirm the observation.", {"R1": {"qty": 82}})
    assert text == "Reported 82 units. A manager must confirm the observation."
    assert (out["claims"], out["removed"]) == (1, 0)
    assert not {"verified", "safe", "stock_confirmed", "reasoning_verified"}.intersection(out)


@pytest.mark.parametrize("excluded_axis", ["run", "kind", "session", "case", "rec_version", "plan_key"])
def test_caller_excluded_scope_results_cannot_be_recovered(excluded_axis, no_network):
    # The caller authorizes rows before constructing this map; the checker must
    # never recover an omitted row through storage or a model-chosen result ID.
    foreign = "R_other_" + excluded_axis
    text, out = checked(f"Foreign [[{foreign}:qty]]. Own [[R1:qty]].",
                        {"R1": {"qty": 82}})
    assert text == f"{REMOVED} Own 82."
    assert (out["claims"], out["removed"]) == (1, 1)
    assert [r["result_id"] for r in out["refs"]] == ["R1"]


@pytest.mark.parametrize("reference", [
    "[[R1:]]", "[[R1:rows.0.qty]", "[[R1:rows.0.qty]]]", "[[[R1:rows.0.qty]]]",
    "[[R1:[[R1:rows.0.qty]]]]", "[[R1:rows.0.qty.__class__]]", "{{0}}",
])
def test_malformed_nested_or_model_written_controls_remove_the_whole_sentence(reference):
    text, out = checked(f"Value {reference}. Still [[R1:rows.0.qty]].")
    assert text == f"{REMOVED} Still 82."
    assert (out["claims"], out["removed"], len(out["refs"])) == (1, 1, 1)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_values_never_become_source_controls(value):
    text, out = checked("Value [[R1:qty]].", {"R1": {"qty": value}})
    assert (text, out["refs"], out["claims"], out["removed"]) == (REMOVED, [], 0, 1)


def test_numeric_checking_has_no_business_authority_or_input_mutation(no_network):
    import copy
    from agent.numbers import check, render

    results = {"R1": {"qty": 82}}
    original = copy.deepcopy(results)
    out = check("Reported [[R1:qty]] units. Managers send, approve, commit, adopt and confirm.", results)
    assert render(out["text"], out["refs"]) == (
        "Reported 82 units. Managers send, approve, commit, adopt and confirm.")
    assert (out["claims"], out["removed"]) == (1, 0)
    assert set(out) == {"text", "refs", "claims", "removed"}
    assert results == original


@pytest.mark.parametrize("sentence,claims,removed", [
    ("Stock 82units; waste 70units; stock 82units.", 3, 0),
    ("Stock 9999units.", 0, 1),
    ("Net USD742.64.", 1, 0),
    ("Net USD742.65.", 0, 1),
    ("Stock 82—70 units; C12/S241/P17/G9.", 2, 0),
    ("Net USD 742.64; Markdown 20%; decide 10:00; stock 82units.", 4, 0),
])
def test_adjacent_values_units_and_identifiers_are_counted_once(sentence, claims, removed):
    text, out = checked(sentence)
    assert (text, out["claims"], out["removed"]) == (REMOVED if removed else sentence, claims, removed)


@pytest.mark.parametrize("sentence,removed", [
    ("Expires 2031-01-06.", 0), ("Expires 2032-01-06.", 1),
    ("Expires 2031-01-07.", 1), ("Expires 2031-02-30.", 1),
    ("Expires 6 January.", 0), ("Expires 6 january.", 0), ("Expires 6 JAN.", 0),
    ("Expires 7 January.", 1),
])
def test_english_dates_keep_the_reference_year_and_month(sentence, removed):
    text, out = checked(sentence)
    assert (text, out["claims"], out["removed"]) == (
        REMOVED if removed else sentence, 0 if removed else 1, removed)


def test_invalid_clock_minutes_do_not_alias_a_valid_deadline():
    text, out = checked("Decide by 09:60.")
    assert (text, out["claims"], out["removed"]) == (REMOVED, 0, 1)


@pytest.mark.parametrize("value,removed", [(10.504, 0), (10.505, 0), (10.506, 1),
                                           (9.499, 1), (9.5, 0), (10.5, 0), (10.501, 1)])
def test_upper_and_integer_half_unit_boundaries(value, removed):
    shown = "10.50" if value > 10.502 else "10"
    sentence = f"Value {shown}."
    text, out = checked(sentence, {"R1": {"qty": value}})
    assert (text, out["claims"], out["removed"]) == (
        REMOVED if removed else sentence, 0 if removed else 1, removed)


def test_rounding_and_signs_follow_the_existing_formatter_without_reordering():
    results = {"R1": {"first": 2.125, "second": 2.375, "third": -2.125, "zero": 0}}
    text, out = checked("[[R1:first]], [[R1:second]], [[R1:third]], [[R1:zero]].", results)
    assert text == "2.12, 2.38, -2.12, 0."
    assert [r["value"] for r in out["refs"]] == [2.125, 2.375, -2.125, 0.0]
    assert (out["claims"], out["removed"]) == (4, 0)


def test_zero_is_valid_only_when_a_numeric_zero_was_supplied():
    assert checked("Stock 0 units.", {"R1": {"qty": 0}})[1]["claims"] == 1
    assert checked("Stock 0 units.", {"R1": {"qty": None}})[0] == REMOVED
    assert checked("Stock 0 units.", {"R1": {"qty": False}})[0] == REMOVED


def test_percent_fraction_and_returned_labels_keep_the_canonical_backstop():
    sentence = "Success 80%; Transfer 82 to S241 (R-02)."
    text, out = checked(sentence, {"R1": {"rate": 0.8, "label": "Transfer 82 to S241 (R-02)"}})
    assert (text, out["claims"], out["removed"]) == (sentence, 2, 0)


def test_equal_values_and_double_digit_controls_keep_exact_reference_order():
    results = {"R1": {"rows": [{"qty": 82} for _ in range(12)]}}
    sentence = ", ".join(f"[[R1:rows.{i}.qty]]" for i in range(12)) + "."
    text, out = checked(sentence, results)
    assert text == ", ".join(["82"] * 12) + "."
    assert [r["path"] for r in out["refs"]] == [f"rows.{i}.qty" for i in range(12)]
    assert (out["claims"], out["removed"]) == (12, 0)


def test_split_utf8_transport_preserves_reference_identity_before_checking():
    import json
    from tests.agent.conftest import sse

    sentence = "S241’s lot—[[R1:rows.0.qty]] units."
    frame = ("event: final\ndata: " + json.dumps({"text": sentence}, ensure_ascii=False) + "\n\n").encode()
    for split in range(1, len(frame)):
        events = sse([frame[:split], frame[split:]])
        text, out = checked(events[0]["data"]["text"])
        assert text == "S241’s lot—82 units."
        assert (out["claims"], out["removed"]) == (1, 0)
        assert out["refs"][0]["path"] == "rows.0.qty"


def test_missing_reference_metadata_never_uses_an_implicit_currency_or_calendar():
    from agent.numbers import check

    sentence = "Net [[R1:net]]. Expires [[R1:expiry_day]]."
    results = {"R1": {"net": 82, "expiry_day": 5}}
    for ref in (None, SimpleNamespace(currency=None, date=None)):
        out = check(sentence, results, ref=ref)
        assert (out["text"], out["refs"], out["claims"], out["removed"]) == (
            f"{REMOVED} {REMOVED}", [], 0, 2)


@pytest.mark.parametrize("sentence", ["Net EUR742.64.", "Net $70.", "Net €70.", "Net USD 7,42.64."])
def test_currency_and_grouping_cannot_bypass_unit_checks(sentence):
    text, out = checked(sentence)
    assert (text, out["claims"], out["removed"]) == (REMOVED, 0, 1)


@pytest.mark.parametrize("value,removed", [(-10.494, 1), (-10.495, 0), (-10.505, 0), (-10.506, 1)])
def test_signed_unicode_numbers_keep_the_same_precision_tolerance(value, removed):
    text, out = checked("Value −10.50.", {"R1": {"qty": value}})
    assert (text, out["claims"], out["removed"]) == (
        REMOVED if removed else "Value −10.50.", 0 if removed else 1, removed)


@pytest.mark.parametrize("sentence,results,claims,removed", [
    ("Transfer 241 units.", {"t1": {"branch": "Store 12", "sku": "S241", "units": 80}}, 0, 1),
    ("Transfer 12 units.", {"t1": {"branch": "Store 12", "sku": "S241", "units": 80}}, 0, 1),
    ("Store 12 has the stock.", {"t1": {"branch": "Store 12", "sku": "S241", "units": 80}}, 1, 0),
    ("Transfer 82 to Lat Phrao (R-02), markdown rest 20%.",
     {"t1": {"label": "Transfer 82 to Lat Phrao (R-02), markdown rest 20%", "pct": 20}}, 2, 0),
])
def test_supervisor_label_contract(sentence, results, claims, removed):
    # As in canonical Task 21, the percentage still needs a numeric source.
    # The transfer quantity has only its verbatim returned label as a source.
    text, out = checked(sentence, results)
    assert (text, out["claims"], out["removed"], out["refs"]) == (
        REMOVED if removed else sentence, claims, removed, [])


@pytest.mark.parametrize("sentence,expected,claims,removed", [
    ("Store 12 has the stock; transfer 12 units.", REMOVED, 0, 1),
    ("Store 12 has the stock. Transfer 12 units.", f"Store 12 has the stock. {REMOVED}", 1, 1),
    ("Store 12 and Store 12 have the stock.", "Store 12 and Store 12 have the stock.", 2, 0),
    ("store 12 has the stock.", REMOVED, 0, 1),
])
def test_label_grounding_is_confined_to_the_matching_occurrence(sentence, expected, claims, removed):
    text, out = checked(sentence, {"t1": {"branch": "Store 12", "sku": "S241", "units": 80}})
    assert (text, out["claims"], out["removed"], out["refs"]) == (expected, claims, removed, [])


def test_text_fields_are_quoted_verbatim_and_their_digits_still_checked():
    results = {"R1": {**RESULTS["R1"], "lot_id": "MLK-042",
                      "label": "Transfer 82 to S241 (R-02), markdown rest 20%"}}
    text, out = checked("Plan [[R1:label]] for lot [[R1:lot_id]] moves [[R1:rows.0.qty]] units.", results)
    assert text == "Plan Transfer 82 to S241 (R-02), markdown rest 20% for lot MLK-042 moves 82 units."
    assert (out["claims"], out["removed"], [r["path"] for r in out["refs"]]) == (3, 0, ["rows.0.qty"])
    text, out = checked("Lot [[R1:lot_id]] has 999 units.", results)
    assert (text, out["removed"]) == (REMOVED, 1)


def test_a_unit_written_beside_a_control_is_shown_once():
    text, _ = checked("Markdown [[R1:options.md20.pct]]% earns USD [[R1:options.md20.net.base]] "
                      "or [[R1:options.md20.net.base]] USD by day [[R1:expiry_day]].")
    assert text.startswith("Markdown 20% earns USD 742.64 or USD 742.64 by ") and "day" not in text
    assert "%%" not in text and "USD USD" not in text and "742.64 USD" not in text


def test_line_breaks_survive_checking():
    text, out = checked("Plan\n- Moves [[R1:rows.0.qty]] units.\n- Invented 999 units.\n\nDone.")
    assert text == f"Plan\n- Moves 82 units.\n{REMOVED}\nDone."
    assert (out["claims"], out["removed"]) == (1, 1)
