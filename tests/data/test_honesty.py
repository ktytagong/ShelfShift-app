"""Honesty checks over every text file in reference/ and data/ (DATA-07, 03-CONTEXT D-11 amended, Seed calibration 8).

USD only: no Thai currency code, word or sign. No claim that the data was calibrated to or matched FreshRetailNet-50K.
Net revenue, never the word profit. The stale project id never appears, and data/*.py hold no project-id literal. The
retailer is never named: the names live in a file outside every repo, named by the SHELFSHIFT_DENYLIST env var, and
that check skips with a reason when it is unset.

The public dataset (05.2 D-03, D-04 as amended 2026-10-17, D-05): sales are public (FreshRetailNet-50K, which may be
named as the source), prices and costs are labelled assumptions, and stock, lots, expiry and deliveries are simulated
and never shown as observed. So reference/public.json carries the mark "Public sales dataset · Simulated operational
scenario · Scenario date: {date}" that the app shows on every page, and no line claims observed, real or actual stock,
lots or inventory unless a negation comes first ("never shown as observed"). "Synthetic" names generated data only, so
the synthetic label stays required where it labels generated data: the synthetic policy file and its assumptions table.
D-05 replaced the rule that every screen shows the synthetic label; this file changed with CLAUDE.md and PROJECT.md.

The patterns are module constants so the report test (03-06) can reuse them. Generated files under data/ are scanned as
later plans add them; this tests directory is outside the walk, so it is the only place the forbidden strings are
spelled.
"""
import json
import os
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]  # the ShelfShift repo
REFERENCE, DATA = ROOT / "reference", ROOT / "data"
SCANNED = (REFERENCE, DATA)

THAI_CURRENCY = re.compile(r"\bTHB\b|\bbaht\b|฿|บาท", re.IGNORECASE)  # code, word, sign, Thai word
# The earlier personal project's ID stays outside the repo: SHELFSHIFT_STALE_PROJECT_ID names it; unset, a stand-in
# built from two parts (so no line spells it) keeps every check running and finds nothing.
STALE_PROJECT_ID = os.environ.get("SHELFSHIFT_STALE_PROJECT_ID", "").strip() or "x-stale-project" + "-unset"
CALIBRATION_CLAIMS = (
    re.compile(r"calibrated\s+to\s+FreshRetailNet", re.IGNORECASE),
    re.compile(r"calibrated\s+to\s+the\s+variability", re.IGNORECASE),
    re.compile(r"match(?:es|ed)\s+FreshRetailNet", re.IGNORECASE),
)
PROFIT = re.compile(r"\bprofit", re.IGNORECASE)  # Seed calibration 8: net revenue after operating costs
PROJECT_ID_LITERAL = re.compile(r"""(?i:project)\w*["']?\s*[:=,(]\s*["'][a-z][a-z0-9-]{4,28}[a-z0-9]["']""")
SYNTHETIC_LABEL = "Synthetic data · hypothetical scenario"  # labels generated data only (05.2 D-03)
LABELLED = (REFERENCE / "policy.json", DATA / "reference" / "assumptions.md")  # the synthetic set's own files
PUBLIC_MARK = "Public sales dataset · Simulated operational scenario · Scenario date: {date}"  # 05.2 D-04 amended
PUBLIC_REFERENCE = REFERENCE / "public.json"
# "observed", "real" or "actual" before stock, lot(s) or inventory, with no negation (never, not, no, nothing,
# without) earlier on the line: "observed stock" is a claim, "never a match to observed stock" is not.
OBSERVED_STOCK_CLAIM = re.compile(
    r"^(?:(?!\b(?:never|not|no|nothing|without)\b).)*?\b(?:observed|real|actual)\s+(?:stock|lots?|inventory)\b",
    re.IGNORECASE)
DENYLIST_ENV = "SHELFSHIFT_DENYLIST"


def text_files(roots=SCANNED):
    """(path, text) for every text file under `roots`, sorted; __pycache__ is skipped and a file with a NUL byte is
    binary. A text file that is not UTF-8 is returned with text None (test_every_text_file_is_utf8 refuses it)."""
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            raw = path.read_bytes()
            if b"\0" in raw:
                continue
            try:
                yield path, raw.decode("utf-8")
            except UnicodeDecodeError:
                yield path, None


def hits(pattern, files=None) -> list[str]:
    """'relative/path:line' for every line a compiled pattern or a plain string matches."""
    found = []
    for path, text in (text_files() if files is None else files):
        for number, line in enumerate((text or "").splitlines(), 1):
            if (pattern in line) if isinstance(pattern, str) else pattern.search(line):
                found.append(f"{path.relative_to(ROOT)}:{number}")
    return found


def test_the_walk_sees_the_reference_files_and_the_data_code():
    seen = {path.relative_to(ROOT).as_posix() for path, _ in text_files()}
    for name in ("reference/policy.json", "reference/world.json", "reference/scenarios.json",
                 "reference/hidden.json", "data/config.py", "data/reference/assumptions.md"):
        assert name in seen, name
    assert not any("__pycache__" in name for name in seen)


def test_every_text_file_is_utf8():
    assert [str(path.relative_to(ROOT)) for path, text in text_files() if text is None] == []


def test_the_patterns_catch_what_they_forbid():
    assert THAI_CURRENCY.search("costs 40 thb") and THAI_CURRENCY.search("40 Baht") and THAI_CURRENCY.search("฿40")
    assert not THAI_CURRENCY.search("THBX bathtub USD")
    assert all(any(p.search(s) for p in CALIBRATION_CLAIMS) for s in (
        "demand Calibrated to FreshRetailNet-50K", "calibrated to the variability of", "it matched FreshRetailNet",
        "the share matches FreshRetailNet"))
    assert not any(p.search("a weekday profile derived from FreshRetailNet-50K") for p in CALIBRATION_CLAIMS)
    assert not any(p.search("Public sales from FreshRetailNet-50K (CC BY 4.0, Dingdong-Inc)") for p in CALIBRATION_CLAIMS)
    for line in ("shows the observed stock of each store", "Real lots from the branch", "the actual inventory on hand",
                 "expiry follows the actual lot"):
        assert OBSERVED_STOCK_CLAIM.search(line), line
    for line in ("stock, lots, expiry and deliveries are simulated and never shown as observed",
                 "never a match to observed stock", "the stock is simulated, not observed", "no real stock is used",
                 "observed sales from the public dataset", "the record's stock against the counted units"):
        assert not OBSERVED_STOCK_CLAIM.search(line), line
    assert PROFIT.search("Profitable") and not PROFIT.search("net revenue after operating costs")
    for line in ('PROJECT = "some-demo-id"', 'bigquery.Client(project="some-demo-id")', '{"project": "some-demo-id"}',
                 "project_id = 'some-demo-id'"):
        assert PROJECT_ID_LITERAL.search(line), line
    assert not PROJECT_ID_LITERAL.search('project = os.environ["GOOGLE_CLOUD_PROJECT"]')


def test_no_thai_currency_code_word_or_sign():
    assert hits(THAI_CURRENCY) == []


def test_the_stale_project_id_never_appears():
    assert hits(STALE_PROJECT_ID) == []


def test_no_claim_that_the_data_was_calibrated_to_or_matched_freshretailnet():
    assert [hit for pattern in CALIBRATION_CLAIMS for hit in hits(pattern)] == []


def test_no_claim_that_stock_was_observed():
    assert hits(OBSERVED_STOCK_CLAIM) == []


def test_the_public_mark_is_in_reference_data():
    mark = json.loads(PUBLIC_REFERENCE.read_text(encoding="utf-8"))["mark"]["text"]
    assert mark["value"] == PUBLIC_MARK
    assert mark["source"] and mark["type"]


def test_no_use_of_the_word_profit():
    assert hits(PROFIT) == []


def test_data_code_holds_no_project_id_literal():
    code = [(path, path.read_text(encoding="utf-8")) for path in sorted(DATA.rglob("*.py"))
            if "__pycache__" not in path.parts]
    assert code, "no data/*.py found"
    assert hits(PROJECT_ID_LITERAL, code) == []


@pytest.mark.parametrize("path", LABELLED, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_the_synthetic_label_is_shown(path):
    assert SYNTHETIC_LABEL in path.read_text(encoding="utf-8")


def denylist() -> list[str]:
    """Load the private list; release gates fail closed when its path is absent."""
    where = os.environ.get(DENYLIST_ENV, "").strip()
    if not where:
        if os.environ.get("SHELFSHIFT_RELEASE_GATE", "").strip() == "1":
            pytest.fail("SHELFSHIFT_DENYLIST is required when SHELFSHIFT_RELEASE_GATE=1", pytrace=False)
        pytest.skip(f"{DENYLIST_ENV} is unset: the retailer denylist is kept outside every repo; set it to the "
                    "file's path to run this check")
    path = pathlib.Path(where).expanduser().resolve()
    assert path.is_file(), f"{DENYLIST_ENV} does not name a readable file"
    assert not path.is_relative_to(ROOT), f"{DENYLIST_ENV} must name a file outside the repo"
    names = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.strip().startswith("#")]
    assert names, f"the {DENYLIST_ENV} file holds no names"
    return names


def test_the_retailer_is_never_named():
    found = []
    for index, name in enumerate(denylist(), 1):
        pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)
        found += [f"denylist entry {index} at {hit}" for hit in hits(pattern)]  # never prints the name itself
    assert found == []
