"""Resolve metric references and remove sentences with untraceable numbers.

The caller supplies only the compact tool results authorized for this request's
run/kind/session/case/version/plan. This module never fetches another result or
grants business authority. Checking a number does not confirm an observation,
food safety or the reasoning around it. Currency/calendar come from ``ref``;
without that metadata, money/date references fail rather than invent a default.

Task 21's backstop compares at half the displayed unit (plus its 1e-9 float
allowance). Only surviving sentences contribute references and claim counts.
"""
from __future__ import annotations

import math
import re
from datetime import date


REMOVED = "⚠ number removed: not traceable to a tool result"
REF = re.compile(r"(?<!\[)\[\[([A-Za-z0-9_-]+):([^\[\]\r\n]+)\]\](?!\])")
SENTENCE = re.compile(r"(\s*\n\s*|(?<=[.!?])[ \t]+)")
# A control already shows its unit (USD 12.50, 20%, Sun 18 Oct); a unit the
# model also wrote beside it would show twice.
UNIT_BEFORE = re.compile(r"(?<![A-Za-z])([A-Z]{3}|[Dd]ay)[ \t]+\{\{(\d+)\}\}")
UNIT_AFTER = re.compile(r"\{\{(\d+)\}\}([ \t]?%|[ \t]+[A-Z]{3}(?![A-Za-z]))")
PLACEHOLDER = re.compile(r"\{\{(\d+)\}\}")
MONEY = {"net", "delta_vs_keep", "regret", "fixed_cost", "per_unit_cost", "price", "transport", "revenue",
         "handling", "disposal", "at_stake", "cancel_cost", "value_at_stake"}
MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
# Currency symbols are recognized so they cannot silently pass as unitless
# numbers. Only the actual reference currency code can ground a money claim.
NUMERAL = re.compile(r"[-−]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?")
AMOUNT = r"[-−]?[0-9][0-9,]*(?:\.[0-9]+)?"
MONTH = (r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
         r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?")
# The right boundary allows adjacent units (82units), while the left boundary
# still excludes digits inside IDs such as S241, SUS-19 and R-02.
NUMBER = re.compile(
    r"(?<!\w)(?:(?P<currency>[A-Z]{3})(?=[ \t0-9−])|(?P<symbol>[$€£¥]))[ \t]*(?P<money>" + AMOUNT + r")"
    r"|(?<![\w\-.])(?P<pct>[-−]?[0-9]+(?:\.[0-9]+)?)\s?%"
    r"|(?<![\w\-:])(?P<time>[0-9]{1,2}):(?P<mm>[0-9]{2})(?![\w:])"
    r"|(?<![\w\-])(?P<iso>[0-9]{4}-[0-9]{2}-[0-9]{2})(?![\w\-])"
    r"|(?<![\w\-])(?P<dd>[0-9]{1,2})\s(?P<mon>(?i:" + MONTH + r"))(?![a-zA-Z])"
    r"|(?<![\w\-.:])(?P<num>" + AMOUNT + r")(?![0-9,%:]|\.[0-9])")


def kind_of(path: list[str]) -> str:
    leaf = path[-1]
    if any(part in MONEY for part in path):
        return "money"
    if leaf.endswith("_min") or leaf == "minute":
        return "time"
    if leaf.endswith("_day") or leaf in ("day", "today"):
        return "day"
    if leaf.endswith("pct"):
        return "pct"
    return "plain"


def fmt(kind: str, value: float, *, ref=None) -> str:
    if kind in ("money", "day"):
        if kind == "money" and (not isinstance(getattr(ref, "currency", None), str) or not ref.currency.strip()):
            raise ValueError("Reference currency is required")
        if kind == "day" and not callable(getattr(ref, "date", None)):
            raise ValueError("Reference currency/calendar is required")
        # Reuse the case page's reference-based formatters; no retailer values
        # or scenario dates are duplicated here.
        from api.cases import date_label, money
        return money(ref, value) if kind == "money" else date_label(int(value), ref)
    if kind == "time":
        return f"{int(value) // 60:02d}:{int(value) % 60:02d}"
    if kind == "pct":
        return f"{value:g}%"
    return f"{value:,.0f}" if abs(value - round(value)) < 0.005 else f"{value:,.2f}"


def _numeric(value) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def lookup(results: dict[str, dict], result_id: str, path: str):
    """Return a numeric leaf in the supplied map; never attributes or storage.

    Colons and spaces inside plan keys are literal. Dots traverse dictionaries
    and nonnegative list indexes only. Missing/null/bool/container is not zero.
    """
    node = _leaf(results, result_id, path)
    return (kind_of(path.split(".")), float(node)) if _numeric(node) else None


def _leaf(results: dict[str, dict], result_id: str, path: str):
    node = results.get(result_id)
    parts = path.split(".")
    if not path or any(not part for part in parts):
        return None
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isascii() and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def leaves(results: dict[str, dict]) -> tuple[dict[str, list[float]], set[str]]:
    """Collect Task 21's numeric kinds and quotable labels, from this map only."""
    nums = {kind: [] for kind in ("money", "time", "day", "pct", "plain")}
    strings = set()

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, path + [str(key)])
                strings.add(str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, path + [str(index)])
        elif _numeric(node) and path:
            nums[kind_of(path)].append(float(node))
        elif isinstance(node, str):
            strings.add(node)

    for result in results.values():
        walk(result, [])
    return nums, strings


def _close(value: float, shown: str, candidates: list[float]) -> bool:
    decimals = len(shown.split(".")[1]) if "." in shown else 0
    return any(abs(value - candidate) <= 0.5 * 10 ** -decimals + 1e-9 for candidate in candidates)


def _in_quoted_label(match: re.Match, labels: set[str]) -> bool:
    """A label grounds only the number inside its verbatim sentence occurrence.

    Digits in an unrelated store name or SKU cannot support a quantity claim.
    Search every occurrence so repeated or overlapping labels retain scope.
    """
    start, end = match.span("num")
    for label in labels:
        if not label:
            continue
        offset = match.string.find(label)
        while offset != -1 and offset <= start:
            if end <= offset + len(label):
                return True
            offset = match.string.find(label, offset + 1)
    return False


def _matches(match: re.Match, nums: dict[str, list[float]], labels: set[str], ref) -> bool:
    groups = match.groupdict()
    if groups["money"] is not None:
        shown = groups["money"].replace(",", "").replace("−", "-")
        return (NUMERAL.fullmatch(groups["money"]) is not None
                and groups["currency"] is not None and groups["currency"] == getattr(ref, "currency", None)
                and _close(float(shown), shown, nums["money"]))
    if groups["pct"] is not None:
        shown = groups["pct"].replace("−", "-")
        return _close(float(shown), shown, nums["pct"] + [v * 100 for v in nums["plain"] if 0 <= v <= 1])
    if groups["time"] is not None:
        return int(groups["mm"]) < 60 and int(groups["time"]) * 60 + int(groups["mm"]) in nums["time"]
    if groups["iso"] or groups["dd"]:
        if not callable(getattr(ref, "date", None)):
            return False
        try:
            if groups["iso"]:
                expected = date.fromisoformat(groups["iso"])
                return any(ref.date(int(v)) == expected for v in nums["day"])
            month, day = MONTHS[groups["mon"].lower()[:3]], int(groups["dd"])
            return any((ref.date(int(v)).month, ref.date(int(v)).day) == (month, day) for v in nums["day"])
        except (ValueError, OverflowError, AttributeError):
            return False
    if NUMERAL.fullmatch(groups["num"]) is None:
        return False
    shown = groups["num"].replace(",", "").replace("−", "-")
    if _in_quoted_label(match, labels):
        return True
    return _close(float(shown), shown, [v for kind in ("plain", "money", "pct", "day") for v in nums[kind]])


def check(text: str, results: dict[str, dict], *, ref=None) -> dict:
    """Return checked text, ordered source controls, claims and removed sentences.

    ``{{n}}`` placeholders are server-owned. Model-written placeholders and
    malformed/nested references remove the entire sentence, without keeping
    any otherwise valid controls from that sentence.
    """
    nums, strings = leaves(results)
    refs, out = [], []
    claims = removed = 0
    pieces = SENTENCE.split(text.strip())
    for index, sentence in enumerate(pieces):
        if index % 2:
            out.append("\n" if "\n" in sentence else " ")
            continue
        if not sentence:
            continue
        ok = "{{" not in sentence and "}}" not in sentence
        local = []

        def resolve(match: re.Match) -> str:
            nonlocal ok
            path = match.group(2).strip()
            hit = lookup(results, match.group(1), path)
            if hit is None:
                label = _leaf(results, match.group(1), path)
                # A text field (name, ID, plan label) is quoted verbatim, never a
                # control; any digits in it still need the label check below.
                if isinstance(label, str) and label.strip() and not any(c in label for c in "[]{}\r\n"):
                    return label
                ok = False
                return ""
            try:
                shown = fmt(*hit, ref=ref)
            except (ValueError, OverflowError, AttributeError):
                ok = False
                return ""
            local.append({"ref": match.group(0), "result_id": match.group(1), "path": path,
                          "kind": hit[0], "value": hit[1], "text": shown})
            return f"{{{{{len(refs) + len(local) - 1}}}}}"

        body = REF.sub(resolve, sentence)

        def unit_of(number: str):
            i = int(number) - len(refs)
            return local[i] if 0 <= i < len(local) else None

        def before(match: re.Match) -> str:
            item, unit = unit_of(match.group(2)), match.group(1)
            same = item is not None and (item["kind"] == "day" and unit.lower() == "day"
                                         or item["kind"] == "money" and unit == getattr(ref, "currency", None))
            return "{{" + match.group(2) + "}}" if same else match.group(0)

        def after(match: re.Match) -> str:
            item, unit = unit_of(match.group(1)), match.group(2).strip()
            same = item is not None and (item["kind"] == "pct" and unit == "%"
                                         or item["kind"] == "money" and unit == getattr(ref, "currency", None))
            return "{{" + match.group(1) + "}}" if same else match.group(0)

        body = UNIT_AFTER.sub(after, UNIT_BEFORE.sub(before, body))
        ok = ok and "[[" not in body and "]]" not in body
        bare = list(NUMBER.finditer(PLACEHOLDER.sub(" ", body)))
        if ok and all(_matches(match, nums, strings, ref) for match in bare):
            refs.extend(local)
            claims += len(local) + len(bare)
            out.append(body)
        else:
            removed += 1
            out.append(REMOVED)
    return {"text": "".join(out).strip(), "refs": refs, "claims": claims, "removed": removed}


def render(text: str, refs: list[dict]) -> str:
    """Replace server-owned controls for plain-text exports and history."""
    return PLACEHOLDER.sub(lambda match: refs[int(match.group(1))]["text"], text)
