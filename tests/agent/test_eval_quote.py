"""Offline quote artifacts agree with their references without activating a template."""
from contextlib import nullcontext
from datetime import date
import hashlib
import json
from types import SimpleNamespace

import pytest

from tests.api import conftest as api_fixtures  # Credentials precede API imports.
from api import preload, refdata, sandbox
from agent.eval import refs


_REAL_RENDER_GUIDE = preload.render_guide


@pytest.fixture
def quote_environment(monkeypatch):
    trip = SimpleNamespace(run_code="R-7", fixed_cost=85.0, origin="S1", dest="S2")
    ref = SimpleNamespace(currency="USD", date=lambda day: date(2026, 10, 18),
                          run_on=lambda code, day: trip if code == "R-7" else None)
    run = dict(run_id="ready-template", kind="TEMPLATE", status="READY", day=0, dataset_version="ds-test")
    guide = dict(template=run["run_id"], dataset_version=run["dataset_version"], case="CASE-7",
                 values=dict(run_code="R-7", weekday="Sun", date="18 Oct 2026"), samples=[],
                 quote=dict(run_code="R-7", amount=85.0, currency="USD"))
    monkeypatch.setattr(refdata, "get", lambda dataset: ref)
    monkeypatch.setattr(preload.db, "tx", lambda: nullcontext(object()))
    monkeypatch.setattr(preload.db, "one", lambda *args, **kwargs: run)
    monkeypatch.setattr(preload.db, "run", lambda *a, **k: pytest.fail("guide must not write the database"))
    monkeypatch.setattr(preload, "render_guide", lambda *a, **k: guide)
    monkeypatch.setattr(sandbox, "active_template", lambda conn: pytest.fail("explicit template must not use ACTIVE"))
    return run, ref, guide


def cli(argv):
    try:
        return preload.main(argv)
    except SystemExit as exc:
        return exc.code


def test_ambiguous_reference_does_not_assert_terms_its_pdf_states(quote_environment, monkeypatch):
    run, ref, _ = quote_environment
    captured = []
    original = preload.quote_pdf

    def capture(*args, **kwargs):
        pdf = original(*args, **kwargs)
        captured.append(pdf)
        return pdf

    monkeypatch.setattr(preload, "quote_pdf", capture)
    inputs = dict(run_code="R-7", case_id="case-7")
    expected = {}
    refs._report_refs(object(), run, "s07", inputs, expected,
                      SimpleNamespace(runs=[ref.run_on("R-7", 0)]), ref,
                      {"api.reports": SimpleNamespace(create=lambda *a: dict(signal_id="signal", revision=1)),
                       "api.impact": SimpleNamespace(preview=lambda *a: dict(cases=[], more=0)),
                       "api.preload": preload})
    assert len(captured) == 1
    assert b"per trip" not in captured[0].lower(), "None pricing_basis requires an unstated basis in the PDF"
    assert b"handling" not in captured[0].lower(), "None includes_handling requires unstated handling in the PDF"
    assert expected["reading_fields"]["pricing_basis"] is None
    assert expected["reading_fields"]["includes_handling"] is None


def test_inactive_template_quote_matches_reference_hash_and_omits_terms(quote_environment, tmp_path):
    run, ref, guide = quote_environment
    out = tmp_path / "guide"
    assert cli(["guide", "--template", run["run_id"], "--case-key", "CASE-7",
                "--ambiguous-quote", "--out", str(out)]) == 0
    pdf = (out / "quote.pdf").read_bytes()
    assert pdf.startswith(b"%PDF-") and b"Synthetic sample" in pdf
    assert b"R-7 S1 to S2" in pdf and b"Sun 18 Oct 2026" in pdf and b"85.00 USD" in pdf
    assert b"per trip" not in pdf.lower() and b"handling" not in pdf.lower()
    inputs = dict(run_code="R-7", case_id="case-7", case_key="CASE-7")
    expected = {}
    reports = SimpleNamespace(create=lambda *a: dict(signal_id="signal", revision=1))
    impact = SimpleNamespace(preview=lambda *a: dict(cases=[], more=0))
    refs._report_refs(object(), run, "s07", inputs, expected,
                      SimpleNamespace(runs=[ref.run_on("R-7", 0)]), ref,
                      {"api.reports": reports, "api.impact": impact, "api.preload": preload})
    assert inputs["quote_pdf"]["sha256"] == hashlib.sha256(pdf).hexdigest()
    assert inputs["quote_pdf"]["amount"] == guide["quote"]["amount"]
    assert expected["reading_fields"]["pricing_basis"] is None
    assert expected["reading_fields"]["includes_handling"] is None


def test_real_eval_attachment_matches_ambiguous_reference_in_clone(quote_environment):
    from agent.eval import steps
    run, _, _ = quote_environment
    quote, expected = preload.quote_for_run(run, "R-7", ambiguous=True)
    ctx = SimpleNamespace(workspace={**run, "run_id": "evaluation-clone", "kind": "SANDBOX"},
                          step={"attachment_ref": "quote_pdf"},
                          inputs={"quote_pdf": {**quote, "sha256": hashlib.sha256(expected).hexdigest()}})
    try:
        actual, mime = steps._attachment(ctx)
    except ValueError as exc:
        pytest.fail(f"The unchanged approved quote must survive clone binding: {exc}")
    assert actual == expected and mime == "application/pdf"
    assert b"R-7 S1 to S2" in actual and b"Sun 18 Oct 2026" in actual
    assert b"per trip" not in actual.lower() and b"handling" not in actual.lower()


@pytest.mark.parametrize("field,value", [("sha256", "0" * 64), ("amount", 86.0), ("currency", "EUR")])
def test_real_eval_attachment_refuses_altered_reference(quote_environment, field, value):
    from agent.eval import steps
    run, _, _ = quote_environment
    quote, pdf = preload.quote_for_run(run, "R-7", ambiguous=True)
    reference = {**quote, "sha256": hashlib.sha256(pdf).hexdigest(), field: value}
    ctx = SimpleNamespace(workspace=run, step={"attachment_ref": "quote_pdf"},
                          inputs={"quote_pdf": reference})
    with pytest.raises(ValueError, match="Evaluation quote differs from the approved reference"):
        steps._attachment(ctx)


@pytest.mark.parametrize("kind,status", [("TEMPLATE", "INITIALIZING"), ("TEMPLATE", "FAILED"),
                                         ("TEMPLATE", "RETIRED"), ("SANDBOX", "ACTIVE")])
def test_guide_refuses_unready_or_non_template_without_output(quote_environment, tmp_path, kind, status):
    run, _, _ = quote_environment
    run.update(kind=kind, status=status)
    out = tmp_path / "not-written"
    assert cli(["guide", "--template", run["run_id"], "--out", str(out)]) == 1
    assert not out.exists()


def test_guide_refuses_unknown_template(quote_environment, monkeypatch, tmp_path):
    monkeypatch.setattr(preload.db, "one", lambda *a, **k: None)
    out = tmp_path / "not-written"
    assert cli(["guide", "--template", "unknown", "--out", str(out)]) == 1
    assert not out.exists()


def test_explicit_case_is_scoped_to_template_and_uses_plan_in_force(quote_environment, monkeypatch):
    from api import selection
    run, _, _ = quote_environment
    case = dict(key="CASE-7", source_branch="S1", sku="P1", lot_id="L1")
    lookups, plans = [], []

    def lookup(conn, sql, **params):
        lookups.append((sql, params))
        return case

    def parse(key):
        plans.append(key)
        return SimpleNamespace(kind=preload.TRANSFER, dest="S2", run_code="R-7")

    monkeypatch.setattr(preload.db, "one", lookup)
    monkeypatch.setattr(preload.cases, "active_rec", lambda *a: dict(result=dict(recommended="stale")))
    monkeypatch.setattr(selection, "plan_in_force", lambda *a: ("effective", [], None))
    monkeypatch.setattr(preload.cases, "parse_plan", parse)
    monkeypatch.setattr(preload.commitment, "checks_in_force", lambda *a: [])
    monkeypatch.setattr(preload.signals, "product_label", lambda *a: "Product · P1")
    try:
        result = preload.guide_values(object(), run, case_key="CASE-7")
    except TypeError:
        result = None
    assert result is not None, "guide must support a scoped explicit case"
    assert result[0] == case and result[1]["run_code"] == "R-7"
    assert plans == ["effective"]
    assert lookups[0][1] == dict(r=run["run_id"], k="CASE-7", s=list(preload.cases.LIVE_STATES))
    assert "run_id=:r" in lookups[0][0] and "key=:k" in lookups[0][0]


def test_missing_explicit_case_does_not_fall_back_to_judge_case(quote_environment, monkeypatch):
    run, _, _ = quote_environment
    monkeypatch.setattr(preload.db, "one", lambda *a, **k: None)
    monkeypatch.setattr(preload, "judge_case", lambda *a: pytest.fail("must not substitute another case"))
    with pytest.raises(ValueError, match="matching live guide case"):
        preload.guide_values(object(), run, case_key="other-template-case")


def test_default_active_guide_keeps_explicit_terms(quote_environment, monkeypatch, tmp_path):
    run, _, guide = quote_environment
    run["status"] = "ACTIVE"
    monkeypatch.setattr(sandbox, "active_template", lambda conn: run)
    out = tmp_path / "guide"
    assert cli(["guide", "--out", str(out)]) == 0
    pdf = (out / "quote.pdf").read_bytes()
    assert b"per trip, handling included" in pdf
    assert b"R-7 S1 to S2" in pdf and b"Sun 18 Oct 2026" in pdf
    assert json.loads((out / "guide-samples.json").read_text()) == guide


@pytest.mark.parametrize("ambiguous,amount,currency", [
    (False, 85.0, "EUR"),
    (True, 85.0, "EUR"),
    (False, 125.0, "USD"),
])
def test_guide_sidecar_describes_actual_quote_and_terms(
        quote_environment, monkeypatch, tmp_path, ambiguous, amount, currency):
    run, ref, _ = quote_environment
    ref.currency = currency
    ref.run_on("R-7", 0).fixed_cost = amount
    values = dict(store="S1", source_store="S1", dest_store="S2", product="P1",
                  lot_id="L1", run_code="R-7", weekday="Sun", date="18 Oct 2026")
    monkeypatch.setattr(preload, "guide_values",
                        lambda *a, **k: ({"key": "CASE-7"}, values))
    monkeypatch.setattr(preload, "render_guide", _REAL_RENDER_GUIDE)
    fixture_samples = preload.samples()
    original_quote = next(s for s in fixture_samples if s["id"] == "quote-pdf")
    assert "$125.00 per trip, handling included" in original_quote["expected"]
    out = tmp_path / "actual-guide"
    argv = ["guide", "--template", run["run_id"], "--case-key", "CASE-7", "--out", str(out)]
    if ambiguous:
        argv.append("--ambiguous-quote")
    assert cli(argv) == 0
    written = json.loads((out / "guide-samples.json").read_text())
    actual = next(s for s in written["samples"] if s["id"] == "quote-pdf")["expected"]
    expected_amount = f"${amount:.2f}" if currency == "USD" else f"{amount:.2f} {currency}"
    assert expected_amount in actual, "Sidecar must describe the actual dataset quote, not fixture pricing"
    assert "R-7" in actual
    if ambiguous:
        assert "pricing basis, handling inclusion and expiry unknown" in actual
        assert "per trip" not in actual and "handling included" not in actual
    else:
        assert "per trip, handling included" in actual
    if amount == 125.0 and currency == "USD" and not ambiguous:
        assert actual == original_quote["expected"].format_map(values)
    for sample, fixture in zip(written["samples"], fixture_samples, strict=True):
        expected = {key: fixture[key] for key in ("id", "use", "kind", "file")}
        expected.update({key: fixture[key].format_map(values)
                         for key in ("title", "text", "expected")})
        if fixture["id"] == "quote-pdf":
            expected["expected"] = actual
        assert sample == expected, "Only the quote-PDF expected description may change"
    _, pdf = preload.quote_for_run(run, "R-7", ambiguous=ambiguous)
    assert (out / "quote.pdf").read_bytes() == pdf
