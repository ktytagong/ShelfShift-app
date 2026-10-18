"""DATASET.md and README carry the dataset's disclosure (05.2 DATA-16; 05.2-CONTEXT D-03, D-04, D-13, D-20).

CC BY 4.0 §3(a) asks for the creator, a copyright notice, a notice that refers to the license and to the disclaimer of
warranties, a link to the material, a note that it was modified and the license's link; §2(a)(6) forbids implying
endorsement. DATASET.md names all of them, the three kinds of data (Public, Assumption, Simulated), the modifications
and the limits. Its assumptions table is the output of `tools/frn.py assumptions --md` between two markers, so it is
generated from reference data and never typed. Every figure it quotes from a report (consistency, network, profile)
is checked here against the committed report, as reported, never tuned; the backtest's stand-in numbers are never
quoted. The honesty patterns of tests/data/test_honesty.py run on DATASET.md and README.md too: no Thai currency, no
calibration claim, no observed-stock claim, no "profit", no project id, and the retailer is never named.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

from tests.data.test_honesty import (  # imported, never respelled
    CALIBRATION_CLAIMS, OBSERVED_STOCK_CLAIM, PROFIT, PROJECT_ID_LITERAL, STALE_PROJECT_ID, THAI_CURRENCY, denylist,
    hits,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]  # the ShelfShift repo
DATASET = ROOT / "DATASET.md"
README = ROOT / "README.md"
REFERENCE = ROOT / "reference"
REPORTS = ROOT / "data" / "reference" / "reports"
BEGIN, END = "<!-- BEGIN ASSUMPTIONS -->", "<!-- END ASSUMPTIONS -->"

SECTIONS = ("Source and license", "Three kinds of data", "Assumptions", "Modifications", "Limits",
            "What the figures are")
# CC BY 4.0 §3(a)(1)(A)(i)-(v), (B), (C) and §2(a)(6), in the research's wording (05.2-RESEARCH "Attribution and
# license"). The dataset's name, creator and links are read from reference/public.json below.
ATTRIBUTION = (
    "FreshRetailNet-50K by Dingdong-Inc (Wang, Gu, Long, Li, Shen, Fu, Zhou, Jiang, 2025)",
    "arXiv:2505.16319",
    "provided as is, without warranties",
    "© Dingdong-Inc, licensed under CC BY 4.0",
    "ShelfShift is not affiliated with or endorsed by Dingdong-Inc.",
)
KINDS = ("**Public**", "**Assumption**", "**Simulated**")  # D-03, named the same way everywhere
MODIFICATIONS = ("keeps its weekday", "units factor", "day-level fields only", "hourly arrays", "display prefix",
                 "unknown price", "stock, lots, expiry and deliveries are simulated")
LIMITS = ("front warehouses", "China", "2024", "holiday flags follow that calendar", "understate demand",
          "stock is simulated", "locations and routes are assumed")
LIVE = re.compile(r"\blive\b", re.IGNORECASE)  # the dataset is never described as live (D-04)


def text(path: pathlib.Path) -> str:
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def reference(name: str) -> dict:
    return json.loads((REFERENCE / name).read_text(encoding="utf-8"))


def report(name: str) -> dict:
    return json.loads((REPORTS / name).read_text(encoding="utf-8"))


def section(markdown: str, title: str) -> str:
    """The body of the '## title' section, up to the next '## ' heading."""
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", markdown, re.M | re.S)
    assert match is not None, f"no '## {title}' section"
    return match.group(1)


def count(n) -> str:
    return f"{int(n):,}"


def share(x) -> str:
    return f"{x:.1%}"


def test_the_assumptions_table_is_generated_from_reference_data():
    out = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "frn.py"), "assumptions", "--md"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    notice = text(DATASET)
    assert notice.count(BEGIN) == 1 and notice.count(END) == 1, "one pair of assumption markers"
    block = notice.split(BEGIN, 1)[1].split(END, 1)[0]
    assert block.strip("\n") == out.strip("\n"), "regenerate with: .venv/bin/python -I tools/frn.py assumptions --md"
    assert BEGIN in section(notice, "Assumptions")
    rows = [line for line in out.splitlines() if line.startswith("| ") and not line.startswith("| Path")]
    assert rows and all(" | " in row for row in rows)


def test_the_notice_carries_every_required_element():
    notice = text(DATASET)
    for title in SECTIONS:
        section(notice, title)
    dataset = reference("public.json")["dataset"]
    for key in ("name", "creator", "license", "license_url", "source_url"):
        assert dataset[key]["value"] in notice, key
    for item in ATTRIBUTION + KINDS:
        assert item in notice, item
    modifications, limits = section(notice, "Modifications"), section(notice, "Limits")
    for item in MODIFICATIONS:
        assert item in modifications, item
    for item in LIMITS:
        assert item in limits, item


def test_the_assumption_rules_are_the_reference_values():
    """D-20: the units-factor rule and the category level, each in its own paragraph, with the reference values."""
    assumptions = section(text(DATASET), "Assumptions").split(BEGIN, 1)[0]
    public, categories = reference("public.json"), reference("categories.json")
    profile = report("public-profile.json")
    units = next(p for p in assumptions.strip().split("\n\n") if p.startswith("**Units factor.**"))
    assert f"factor of {public['units']['factor']['value']}" in units
    assert public["units"]["rule"]["value"] in units
    assert f"{profile['units_factor']['lattice_share']:.0%}" in units
    level = categories["level"]["value"]
    category = next(p for p in assumptions.strip().split("\n\n") if p.startswith("**Category level.**"))
    assert f"`{level}`" in category and f"{profile['categories'][level]['codes']} codes" in category


def test_the_modifications_carry_the_reference_values():
    modifications = section(text(DATASET), "Modifications")
    calendar = reference("public.json")["calendar"]
    assert f"+{calendar['shift_days']['value']} days" in modifications
    codes = reference("public.json")["codes"]
    for kind in ("city", "store", "product", "category"):
        assert f"`{codes[kind]['value']}`" in modifications, kind
    unknown = reference("public.json")["unknown_price"]
    assert f"{unknown['below_or_equal']['value']}" in modifications and f"{unknown['above']['value']}" in modifications


def test_the_figures_are_the_reports_as_reported():
    notice = text(DATASET)
    consistency, network = report("public-consistency.json"), report("public-network.json")
    profile, backtest = report("public-profile.json"), report("public-backtest.json")
    figures = [
        count(consistency["series"]), count(consistency["series_days"]),
        count(consistency["public_stockout_days"]), count(consistency["sim_stockout_days"]),
        count(consistency["agree_stockout_days"]),
        share(consistency["agree_share_of_public_stockout_days"]), share(consistency["agree_share_of_sim_stockout_days"]),
        count(consistency["eval_days_short_without_topup"]["series_days"]),
        count(consistency["eval_days_short_without_topup"]["of_series_days"]),
        share(consistency["eval_days_short_without_topup"]["share"]),
        count(consistency["unknown_price_rows"]),
        count(network["stores"]), count(network["cities"]), count(network["unmapped_stores"]),
        count(profile["products"]), share(profile["stockout"]["share"]),
    ]
    assert consistency["sold_equals_public"] is True  # the hard invariant the notice states (D-16)
    for figure in figures:
        assert figure in notice, figure
    assert backtest["label"] in notice  # the label, never the stand-in's numbers ("never quoted")
    for value in (backtest["wape"], backtest["baseline_wape"]):
        assert share(value) not in notice and str(value) not in notice


def test_the_readme_keeps_the_data_section_before_the_release_disclosure():
    readme = text(README)
    headings = re.findall(r"^## (.+)$", readme, re.M)
    # the release README also carries the deploy branch's run, test and deploy sections first (06); the data section
    # still comes right before the release disclosure, which ends the README
    assert headings[-2:] == ["Data", "What is real, simulated and not yet proven"], headings
    data = section(readme, "Data")
    assert "[DATASET.md](DATASET.md)" in data
    dataset = reference("public.json")["dataset"]
    for key in ("name", "creator", "license_url"):
        assert dataset[key]["value"] in data, key
    assert "Stock, lots, expiry and deliveries are simulated" in data


def test_the_notice_and_the_readme_keep_the_honesty_rules():
    files = [(path, text(path)) for path in (DATASET, README)]
    patterns = (THAI_CURRENCY, OBSERVED_STOCK_CLAIM, PROFIT, PROJECT_ID_LITERAL, STALE_PROJECT_ID, LIVE,
                *CALIBRATION_CLAIMS)
    assert [hit for pattern in patterns for hit in hits(pattern, files)] == []


def test_the_retailer_is_never_named_in_the_notice_or_the_readme():
    files = [(path, text(path)) for path in (DATASET, README)]
    found = []
    for index, name in enumerate(denylist(), 1):  # skips with a reason when the denylist env var is unset
        pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)
        found += [f"denylist entry {index} at {hit}" for hit in hits(pattern, files)]  # never prints the name
    assert found == []


# --- About says the same (05.2-20 Task 2) ---------------------------------------------------------------------------
# web/src/copy.ts exports the About paragraphs as plain string constants (one literal, or literals joined with +);
# each must stand word for word in DATASET.md, so a judge reading About and a developer reading the repository get the
# same account of the data.
COPY = ROOT / "web" / "src" / "copy.ts"
ABOUT_NAMES = ("ABOUT_SOURCE", "ABOUT_KINDS", "ABOUT_MODIFICATIONS", "ABOUT_LIMITS")
ABOUT_CONST = re.compile(r'^export const (ABOUT_[A-Z_]+) =\s*((?:"(?:[^"\\\n]|\\.)*"\s*\+?\s*)+);', re.M)
STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"')


def about_strings() -> dict[str, str]:
    """{name: text} of every exported ABOUT_* string constant in copy.ts."""
    return {name: "".join(json.loads(piece) for piece in STRING.findall(body))
            for name, body in ABOUT_CONST.findall(text(COPY))}


def test_about_and_dataset_md_say_the_same():
    about = about_strings()
    assert sorted(about) == sorted(ABOUT_NAMES), sorted(about)
    notice = text(DATASET)
    for name, value in about.items():
        assert value.strip(), name
        assert value in notice, f"{name} is not word for word in DATASET.md"
    assert about["ABOUT_SOURCE"] in section(notice, "Source and license")
    assert about["ABOUT_KINDS"] in section(notice, "Three kinds of data")
    assert about["ABOUT_MODIFICATIONS"] in section(notice, "Modifications")
    assert about["ABOUT_LIMITS"] in section(notice, "Limits")


def test_about_shows_the_paragraphs_and_the_public_dataset_sentence():
    pages = text(ROOT / "web" / "src" / "pages.tsx")
    for name in ABOUT_NAMES:
        assert re.search(rf"\{{?\b{name}\b", pages), f"About does not show {name}"
    # Phase 11 D-07 reorganizes these same paragraphs under Simulated and The dataset.
    for title in ("Real now", "Simulated", "The dataset"):
        assert f'title="{title}"' in pages, title
    for title in ("The data", "Modifications", "Limits"):
        assert f'>{title}</h3>' in pages, title
    assert "What's real and what's synthetic" not in pages
    assert '"Public sales from FreshRetailNet-50K, with simulated stock and labelled assumptions"' in pages
    assert "Stock, lots and deliveries are simulated from the public sales." in pages
    assert "not floored" not in pages  # the fill floor applies to the public set (05.2 D-14)
