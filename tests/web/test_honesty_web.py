"""Honesty and exposure checks over web/src and web/index.html (DATA-07, UI-01; research Pitfall 11). No database.

The patterns are Phase 3's module constants, imported from tests/data/test_honesty.py and never respelled, so one
file spells each forbidden string. node_modules and dist are never scanned (they are not ours). Non-test sources also
hold no hard-coded currency: every amount is formatted from the session currency, so no symbol and no currency code
is typed in the code (05-UI-SPEC "Money"). No source uses React's raw-HTML prop: engine, reason and reply strings
are rendered as text.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from tests.data.test_honesty import (  # imported, never respelled
    DENYLIST_ENV, PROFIT, PROJECT_ID_LITERAL, STALE_PROJECT_ID, SYNTHETIC_LABEL, THAI_CURRENCY, denylist,
)

pytestmark = pytest.mark.web

ROOT = pathlib.Path(__file__).resolve().parents[2]  # the ShelfShift repo
WEB = ROOT / "web"

# A dollar sign before a digit or before a template interpolation ("$5", "$${x}"), a lone dollar sign as a string,
# another hard-coded currency sign, or a common currency code written as a string literal.
CURRENCY_LITERAL = re.compile(
    r"""\$(?=\d)|\$(?=\$\{)|(["'`])\$\1|[€£¥₹₩₫]"""
    r"""|(["'`])(?:USD|EUR|GBP|JPY|CNY|THB|SGD|MYR|IDR|VND|PHP|INR|AUD|CAD|CHF|HKD|KRW|NZD|TWD)\2""")
RAW_HTML = re.compile(r"dangerouslySetInnerHTML")


def web_files() -> list[tuple[pathlib.Path, str]]:
    """(path, text) for every file under web/src plus web/index.html, sorted; never node_modules or dist."""
    paths = sorted(p for p in (WEB / "src").rglob("*") if p.is_file()) + [WEB / "index.html"]
    return [(p, p.read_text(encoding="utf-8")) for p in paths]


def sources() -> list[tuple[pathlib.Path, str]]:
    """The files that ship: everything scanned except the node unit tests."""
    return [(p, t) for p, t in web_files() if not p.name.endswith(".test.ts")]


def hits(pattern, files) -> list[str]:
    """'web/...:line' for every line a compiled pattern or a plain string matches."""
    found = []
    for path, text in files:
        for number, line in enumerate(text.splitlines(), 1):
            if (pattern in line) if isinstance(pattern, str) else pattern.search(line):
                found.append(f"{path.relative_to(ROOT)}:{number}")
    return found


def test_the_walk_sees_web_src_and_index_html_but_not_dependencies_or_the_build():
    seen = {p.relative_to(ROOT).as_posix() for p, _ in web_files()}
    for name in ("web/index.html", "web/src/main.tsx", "web/src/api.ts", "web/src/shell.tsx", "web/src/api.test.ts"):
        assert name in seen, name
    assert not any("node_modules" in name or name.startswith("web/dist") for name in seen)
    assert "web/src/api.test.ts" not in {p.relative_to(ROOT).as_posix() for p, _ in sources()}


def test_the_currency_pattern_catches_what_it_forbids():
    for line in ('const p = "$5.00";', "`$${amount}`", "label = '$'", '<b>{"$"}</b>', "x = '€'", 'currency ?? "USD"',
                 "c = 'THB'", "`EUR`"):
        assert CURRENCY_LITERAL.search(line), line
    for line in ("`Day ${session.day} · ${session.clock}`", 'replace(/^#/, "")', "currency: session.currency",
                 'method: "POST"', 'kind: "KEEP"', "const used = 'USDA'"):
        assert not CURRENCY_LITERAL.search(line), line


def test_no_thai_currency_code_word_or_sign():
    assert hits(THAI_CURRENCY, web_files()) == []


def test_no_use_of_the_word_profit():
    assert hits(PROFIT, web_files()) == []


def test_the_stale_project_id_never_appears():
    assert hits(STALE_PROJECT_ID, web_files()) == []


def test_no_project_id_literal():
    assert hits(PROJECT_ID_LITERAL, web_files()) == []


def test_no_hard_coded_currency_symbol_or_code_in_sources():
    assert hits(CURRENCY_LITERAL, sources()) == []


def test_no_raw_html_prop():
    assert hits(RAW_HTML, web_files()) == []


# The dataset mark comes from the session (05.2 D-03, D-04 as amended 2026-10-17; D-05 replaced the old check that the
# synthetic label was typed in the app). No file under web/src spells the old label or the public mark: the shell's
# DatasetMark shows copy.datasetMark(session), which reads session.label (reference data) and fills its {date} from
# data_as_of with format.markDate. Only the gate's no-session line, "Public sales dataset", is typed.
PUBLIC_MARK_PART = "Simulated operational scenario"  # a part of the reference mark that no web source may type


def test_the_dataset_mark_comes_from_the_session():
    assert hits(SYNTHETIC_LABEL, web_files()) == []
    assert hits(PUBLIC_MARK_PART, sources()) == []
    shell = (WEB / "src" / "shell.tsx").read_text(encoding="utf-8")
    copy = (WEB / "src" / "copy.ts").read_text(encoding="utf-8")
    assert "function DatasetMark" in shell and "datasetMark(value.session)" in shell
    mark = re.search(r"export function datasetMark\(s: [^)]*\): string \{(.*?)\n\}", copy, re.S)
    assert mark is not None, "copy.datasetMark is missing"
    assert "s.label" in mark.group(1) and "markDate(s.data_as_of)" in mark.group(1)


def test_the_retailer_is_never_named():
    names = denylist()  # skips with a reason when the denylist env var is unset; fails when it names no readable file
    found = []
    for index, name in enumerate(names, 1):
        pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)
        found += [f"denylist entry {index} at {hit}" for hit in hits(pattern, web_files())]  # never prints the name
    assert found == [], f"a name from the {DENYLIST_ENV} file appears in web/"


# --- the product's words (06.1 D-03, D-14, D-36, D-61; 06.1-UI-SPEC "Removed strings", "On-screen terms") -------------
# The UI-SPEC's removed strings are written once, here, by design: this list is what the scan forbids everywhere else
# under web/src, comments included. A string with a placeholder is matched by its fixed part.

REMOVED_STRINGS = (
    "Open the demo", "Preparing your sandbox", "Switch seat in the demo panel", "Press Next day in the demo panel",
    "The demo moved on since you loaded this page", "Could not reach the demo server",
    "Your sandbox · changes affect only you", "Acting as", "Next day", "Reset my sandbox", "Start here",
    "startHereItems", "Wait until", "Hold expires in", "min left", "Synthetic data · hypothetical scenario",
    "Pilot · synthetic data", "Data as of", "Sample dataset",
)
REMOVED_TAGS = ('"You"', '"Suggested"')  # the "You" tag and the "Suggested" pill, as string literals
# 07-21: a removed string allowed in one file, each with its reason. "min left" was removed with 06.1's booking-hold
# countdown; 06.1 D-19 keeps one countdown, Phase 7's request deadline bar, whose text "{N} min left" is locked by the
# user (07-CONTEXT D-06). Only copy.ts (copy.minutesLeftText, 07-21) and taskview.ts (07-19) may hold it; anywhere else
# it still fails.
REMOVED_EXCEPTIONS = {
    ("web/src/copy.ts", "min left"): "the request deadline bar's minutes (07-CONTEXT D-06; 06.1 D-19)",
    ("web/src/taskview.ts", "min left"): "the Tasks deadline bar's minutes (07-19; 07-CONTEXT D-06; 06.1 D-19)",
}

VISIBLE_TEXT = pathlib.Path(__file__).resolve().parent / "visible_text.mjs"


def screen_texts(paths: list[pathlib.Path] | None = None) -> list[dict]:
    """Every string literal, template literal and JSX text a shipped web/src source (or the given files) can show, by
    file and line (visible_text.mjs, the TypeScript parser from web/node_modules): comments, import paths, type-level
    literals, object keys and non-visible JSX attributes are not on screen and are left out."""
    import json
    import subprocess

    if paths is None:
        paths = [p for p, _ in sources() if p.suffix in (".ts", ".tsx")]
    files = [str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p) for p in paths]
    out = subprocess.run(["node", str(VISIBLE_TEXT), *files], cwd=ROOT, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def prose(text: str) -> bool:
    """Copy rather than a key, code or path: it holds a space, or it starts with a capital letter."""
    return " " in text or text[:1].isupper()


# The demo's words on screen (D-03; product-not-demo): none in any visible string. Identifiers such as demo_mode or
# /api/demo/… are not prose and are not matched.
DEMO_WORDS = re.compile(r"(?i)\b(demo|sandbox(es)?|seats?)\b|\bNext day\b")

# D-61's internal terms, as visible copy (06.1-UI-SPEC "On-screen terms"): Case → Stock decision, Evidence request →
# Stock check, Board → Decision board, Work order → Transfer work order (or Markdown work order), Run → Scheduled
# truck run. API names, type names and identifiers are not prose and are skipped.
INTERNAL_TERMS = (
    ("Case", re.compile(r"(?i)\bcases?\b")),
    ("Evidence request", re.compile(r"(?i)\bevidence request(s|ed)?\b")),
    ("Board alone", re.compile(r"(?i)(?<!decision )\bboard\b")),
    ("Work order alone", re.compile(r"(?<!Transfer )(?<!Markdown )\bWork orders?\b")),
    ("Run alone", re.compile(r"\bRuns?\b")),
)
# The few visible strings the UI-SPEC itself names with a generic word, each with its reason: (file, exact text).
TERM_EXCEPTIONS = {
    ("web/src/aboutview.ts", "Cloud Run serves the app, Cloud SQL for PostgreSQL keeps each workspace, and Cloud Storage holds the release files"): "Phase 11 D-07 names the Cloud Run service",
    ("web/src/aboutview.ts", "Run recorded · not published"): "Phase 11 D-08's locked check-status vocabulary",
    ("web/src/decision/assistant.tsx", "CASE"): "the API's assistant scope value, compared in code and never shown (09-17)",
    ("web/src/copy.ts", "Report an observation for this case."):
        "10-UI-SPEC S4 locked title for a report opened from the case ask box",
    ("web/src/copy.ts", "Run on an earlier assistant version · not representative of the current app"):
        "10-UI-SPEC S7: a dated evaluation run, not a scheduled truck run",
    **{("web/src/evalsummary.tsx", text): "10-UI-SPEC S7: evaluation repetitions and failure references"
       for text in ("Run", "Run 1", "Run 2", "Run 3")},
    ("web/src/pages/board.tsx", "Board"): 'the "Board | Table" layout switch (UI-SPEC "Decision board" toolbar)',
    ("web/src/pages/schedule.tsx", "Work orders"): "the Schedule's tab, mixing transfer and markdown work (UI-SPEC OQ-3)",
    ("web/src/pages/schedule.tsx", "Work order"): "the Schedule's Group by, mixing both kinds (UI-SPEC OQ-3)",
    ("web/src/pages/schedule.tsx", "Work order / activity"): "the Schedule grid's first column over both kinds (OQ-3)",
    ("web/src/decision/steps/execute.tsx", "Work orders"):
        "Execute's list of a decision's work orders, transfer and markdown together (OQ-3; proposed, for the user)",
}


def test_no_removed_string_ships():
    found = [f"{s!r} at {hit}" for s in REMOVED_STRINGS + REMOVED_TAGS for hit in hits(s, sources())
             if (hit.rsplit(":", 1)[0], s) not in REMOVED_EXCEPTIONS]
    assert found == [], found


def test_no_demo_word_on_screen():
    found = [f"{e['file']}:{e['line']} {e['text']!r}" for e in screen_texts()
             if prose(e["text"]) and DEMO_WORDS.search(e["text"])]
    assert found == [], found


def test_internal_terms_stay_off_screen():
    found = []
    for e in screen_texts():
        if not prose(e["text"]) or (e["file"], e["text"]) in TERM_EXCEPTIONS:
            continue
        found += [f"{name} at {e['file']}:{e['line']} {e['text']!r}" for name, pattern in INTERNAL_TERMS
                  if pattern.search(e["text"])]
    assert found == [], found


def test_the_screen_scan_reads_copy_but_not_comments_paths_types_keys_or_class_names(tmp_path):
    probe = tmp_path / "probe.tsx"
    probe.write_text(
        'import { x } from "./copy.ts";\n'
        "// Start here, in a comment\n"
        'type Role = "manager" | "hq";\n'
        'const keyed = { "Key words": 1 };\n'
        'export const A = "Shown words";\n'
        "export const B = `Hello ${x} world`;\n"
        'export function C() { return <p className="text-ink" aria-label="Named here">Seen text</p>; }\n',
        encoding="utf-8")
    assert [e["text"] for e in screen_texts([probe])] == ["Shown words", "Hello {} world", "Named here", "Seen text"]


# Words more than one page uses are defined once (06.1-30, R-2): in vocab.ts (or format.ts, ui.tsx), which copy.ts
# re-exports, so copy.ts stays the one import path for copy. Each name below is declared in exactly one shipped file.
HOISTED = ("CAPACITY_HOLD_EXPIRED", "RECEIPT_DISCREPANCY_BADGE", "STOCK_CHECK_REQUESTED", "MISSING_DATA", "DECISION_CHIP",
           "MOVEMENT_CHIP", "DISCREPANCY", "SCHEDULE_STATUS", "TAKE_OWNERSHIP_HELP", "OWNED_BY", "RELOAD_DECISION",
           "CLEAR_FILTERS", "ALL_BRANCHES", "IN_STORE", "LOGISTICS", "receiptLine", "PROVIDER_NAMES",
           "ANSWER_STOCK_CHECK", "CONFIRM_COUNT", "BACK_TO_BOARD", "shortDate", "TRY_AGAIN", "ChipLook",
           "decisionDate", "named", "LOADING_HISTORY", "HISTORY_EMPTY", "LinkButton", "Segmented", "LabelledSelect")


def test_copy_re_exports_vocab_and_each_hoisted_word_is_defined_once():
    copy = (WEB / "src" / "copy.ts").read_text(encoding="utf-8")
    assert copy.count('export * from "./vocab.ts"') == 1
    for name in HOISTED:
        pattern = re.compile(r"^(?:export )?(?:const|function|type) " + re.escape(name) + r"\b")
        where = hits(pattern, sources())
        assert len(where) == 1, (name, where)


# --- tests/web and build/web's commit messages (05-03 UI-01 privacy; G15) ---------------------------------------------
# The same pattern constants, over the browser suite's own sources and every commit message reachable from build/web.
# A service-account address is matched by its shape only; the domain is assembled from two parts so this file never
# spells one. A hit is reported by pattern name and path (or commit), never by the matched text.

SERVICE_ACCOUNT = re.compile(r"[\w.-]+@[\w.-]*" + "gservice" + r"account\.com", re.IGNORECASE)
TESTS_WEB = ROOT / "tests" / "web"
PRIVACY = (("STALE_PROJECT_ID", STALE_PROJECT_ID), ("PROJECT_ID_LITERAL", PROJECT_ID_LITERAL),
           ("SERVICE_ACCOUNT", SERVICE_ACCOUNT))


def suite_files() -> list[tuple[pathlib.Path, str]]:
    """(path, text) for every .py and fixture file under tests/web, never __pycache__."""
    return [(p, p.read_text(encoding="utf-8")) for p in sorted(TESTS_WEB.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts]


def commit_messages() -> list[tuple[str, str]]:
    """(short hash, message) for every commit reachable from build/web (HEAD when the branch name is absent)."""
    import subprocess

    ref = "build/web"
    probe = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "-q", ref], capture_output=True, text=True)
    if probe.returncode != 0:
        ref = "HEAD"
    out = subprocess.run(["git", "-C", str(ROOT), "log", ref, "--format=%h%x00%B%x1e"], capture_output=True,
                         text=True, check=True).stdout
    found = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if record:
            sha, _, body = record.partition("\x00")
            found.append((sha, body))
    return found


def test_the_service_account_pattern_catches_what_it_forbids():
    domain = "gservice" + "account.com"
    for line in (f"run-sa@some-proj.iam.{domain}", f"12345-compute@developer.{domain}"):
        assert SERVICE_ACCOUNT.search(line), line
    for line in ("noreply@anthropic.com", "the service account is named in deploy/env.sh"):
        assert not SERVICE_ACCOUNT.search(line), line


def test_the_walk_sees_tests_web_and_the_commit_log():
    seen = {p.relative_to(ROOT).as_posix() for p, _ in suite_files()}
    for name in ("tests/web/conftest.py", "tests/web/test_honesty_web.py", "tests/web/test_board.py"):
        assert name in seen, name
    assert not any("__pycache__" in name for name in seen)
    assert len(commit_messages()) > 1, "the commit log of build/web is empty"


def test_tests_web_hold_no_project_id_or_service_account():
    found = [f"{name} at {hit}" for name, pattern in PRIVACY for hit in hits(pattern, suite_files())]
    assert found == [], found  # names and paths only, never the matched text


def test_build_web_commit_messages_hold_no_project_id_or_service_account():
    found = []
    for sha, body in commit_messages():
        for name, pattern in PRIVACY:
            if (pattern in body) if isinstance(pattern, str) else pattern.search(body):
                found.append(f"{name} in commit {sha}")
    assert found == [], found  # names and commits only, never the matched text


# Phase 11 D-06/D-07/D-08: public prose and the repository must tell the same bounded story.
def test_no_unsupported_demand_bias_figures():
    found = []
    for path, content in [*web_files(), (ROOT / "README.md", (ROOT / "README.md").read_text())]:
        for number, line in enumerate(content.splitlines(), 1):
            if "overstates daily demand" in line.lower() or ("1.15" in line and "0.25" in line):
                found.append(f"{path.relative_to(ROOT)}:{number}")
    assert found == [], found


def test_ai_evaluation_is_never_called_accuracy():
    content = (ROOT / "web/src/pages.tsx").read_text()
    ai_block = content.split('data-about-section="ai-evaluation"', 1)[1].split('data-about-section="weekcheck"', 1)[0]
    assert not re.search(r"accuracy|%", ai_block, re.I)
    view = (ROOT / "web/src/aboutview.ts").read_text()
    ai_literals = re.findall(r'export const AI_[A-Z_]+ = "([^"\n]*)"', view)
    assert ai_literals
    assert not any(re.search(r"accuracy|%", item, re.I) for item in ai_literals)
    assert not any(re.search(r"AI evaluation[^.\n]*(?:accuracy|%)", e["text"], re.I) for e in screen_texts())


def test_no_claim_that_physical_stores_stock_out_less():
    claim = re.compile(r"(?:physical|cash.and.carry|brick.and.mortar)[^.\n]*stock(?:s|ed)?\s*out\s*less", re.I)
    found = [f"{p.relative_to(ROOT)}:{i}" for p, content in [*web_files(), (ROOT / "README.md", (ROOT / "README.md").read_text())]
             for i, line in enumerate(content.splitlines(), 1) if claim.search(line)]
    assert found == [], found


def test_readme_disclosure():
    import json
    readme = (ROOT / "README.md").read_text()
    view = (ROOT / "web/src/aboutview.ts").read_text()
    block = view.split("export const WEEKCHECK_TEXT = [", 1)[1].split("] as const", 1)[0]
    paragraphs = [json.loads(item) for item in re.findall(r'"(?:[^"\\]|\\.)*"', block)]
    assert len(paragraphs) == 3
    for paragraph in paragraphs:
        assert paragraph in readme
    for phrase in ["Public sales from FreshRetailNet-50K, with simulated stock and labelled assumptions",
                   "Synthetic 5-seed engineering check", "Not yet proven", "personal sign-in", "shared organization workspace",
                   "backups", "point-in-time recovery", "tested restore"]:
        assert phrase in readme, phrase
    assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", readme)
