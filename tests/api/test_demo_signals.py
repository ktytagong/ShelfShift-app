"""The reviewer guide's material in data/demo_signals/ (SIG-05, 07-CONTEXT D-03, D-18, D-19).

samples.json holds the guide's texts for the normal forms: English only, no retailer name, and every store, product,
lot and run written as a {placeholder} that the release template fills with the dataset's codes. Every other file in
the folder must pass the upload rules (spec §4: PNG, JPEG or PDF by content, at most 5 MB) and carry no metadata
segment that can hold a location (an APP1 Exif or XMP segment in a JPEG, an eXIf chunk in a PNG).

The three team photos are the user's own (D-19) and the quote screenshot follows the user's design (D-34, D-37);
with DEMO_SIGNALS_STRICT=1 their presence is required, otherwise the presence test skips and names what is missing.
This file uses no database fixture.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import struct
import unicodedata

import pytest

from api import config

ROOT = pathlib.Path(__file__).resolve().parents[2]
FOLDER = ROOT / "data" / "demo_signals"
SAMPLES = FOLDER / "samples.json"
SEED = ROOT / "data" / "reference" / "seed.json"

MAX_BYTES = config.UPLOAD_MAX_BYTES  # spec §4 "up to 5 MB": the uploads.max_bytes setting (07-07)
REQUIRED_ASSETS = ("milk-slot.jpg", "crates-expiry.jpg", "blurry.jpg", "quote-r02-chat.png")  # the photos, the quote (D-34)
FIELDS = {"id", "use", "kind", "title", "text", "file", "expected"}
USES = {"report", "answer"}
KINDS = {"CAPACITY", "DEMAND_EVENT", "TRANSPORT", "STOCK_COUNT", "EXPIRY", "CONDITION", "NONE"}
PLACEHOLDERS = {"store", "dest_store", "source_store", "product", "lot_id", "run_code", "weekday", "date"}
PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
FILE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*\.(?:jpg|png|pdf)$")
DENYLIST_ENV = "SHELFSHIFT_DENYLIST"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
PDF_SIGNATURE = b"%PDF-"


def samples() -> list[dict]:
    return json.loads(SAMPLES.read_text(encoding="utf-8"))


def sample_strings(entry: dict) -> list[str]:
    return [entry[key] for key in ("title", "text", "expected")]


def latin_only(text: str) -> bool:
    """Letters must be Latin script; other characters must be ASCII, Latin-1, Latin Extended or general
    punctuation (the middle dot, dashes and curly quotes). Thai, Japanese or any other script fails."""
    for char in text:
        if unicodedata.category(char).startswith("L"):
            if "LATIN" not in unicodedata.name(char, ""):
                return False
        elif not (char.isascii() or 0x00A0 <= ord(char) <= 0x024F or 0x2000 <= ord(char) <= 0x206F):
            return False
    return True


def seed_names() -> list[str]:
    """Store and product names from the synthetic seed, read from reference data so the test never types them."""
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    return list(seed["branches"]) + [product["name"] for product in seed["products"]]


def denylist() -> list[str]:
    """The retailer names from the file SHELFSHIFT_DENYLIST names; empty (never read) when the variable is unset."""
    where = os.environ.get(DENYLIST_ENV, "").strip()
    if not where:
        return []
    path = pathlib.Path(where).expanduser().resolve()
    assert path.is_file(), f"{DENYLIST_ENV} does not name a readable file"
    assert not path.is_relative_to(ROOT), f"{DENYLIST_ENV} must name a file outside the repo"
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def jpeg_metadata_segments(data: bytes) -> list[str]:
    """The APP1 segments ('Exif' or 'XMP') before the image data; 'malformed' when the header can't be walked."""
    found, at = [], 2
    while at + 4 <= len(data):
        if data[at] != 0xFF:
            return found + ["malformed"]
        marker = data[at + 1]
        if marker == 0xFF:  # fill byte
            at += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # markers without a length
            at += 2
            continue
        if marker in (0xDA, 0xD9):  # start of scan or end of image: no metadata segment follows
            return found
        (length,) = struct.unpack(">H", data[at + 2:at + 4])
        if length < 2:
            return found + ["malformed"]
        payload = data[at + 4:at + 2 + length]
        if marker == 0xE1:
            found.append("Exif" if payload.startswith(b"Exif\0") else "XMP")
        at += 2 + length
    return found + ["malformed"]


def png_has_exif(data: bytes) -> bool:
    at = len(PNG_SIGNATURE)
    while at + 8 <= len(data):
        (length,) = struct.unpack(">I", data[at:at + 4])
        kind = data[at + 4:at + 8]
        if kind == b"eXIf":
            return True
        if kind == b"IEND":
            return False
        at += 12 + length
    return False


def asset_problems(path: pathlib.Path) -> list[str]:
    """Why a file breaks the upload rules (type by content, size, metadata); empty when it passes."""
    data = path.read_bytes()
    problems = []
    if len(data) > MAX_BYTES:
        problems.append(f"{path.name}: {len(data)} bytes, over the {MAX_BYTES}-byte limit")
    if data.startswith(JPEG_SIGNATURE):
        segments = jpeg_metadata_segments(data)
        if segments:
            problems.append(f"{path.name}: JPEG carries {', '.join(segments)} metadata; export it without metadata")
    elif data.startswith(PNG_SIGNATURE):
        if png_has_exif(data):
            problems.append(f"{path.name}: PNG carries an eXIf chunk; export it without metadata")
    elif not data.startswith(PDF_SIGNATURE):
        problems.append(f"{path.name}: not PNG, JPEG or PDF by its content")
    return problems


def folder_assets(folder: pathlib.Path) -> list[pathlib.Path]:
    return sorted(path for path in folder.iterdir()
                  if path.is_file() and path.name not in {"samples.json", ".gitkeep", ".DS_Store"})


def test_samples_are_english_and_use_placeholders():
    entries = samples()
    assert len(entries) == 10
    assert len({entry["id"] for entry in entries}) == len(entries), "sample ids repeat"
    names = seed_names()
    retailer = denylist()
    for entry in entries:
        assert set(entry) == FIELDS, entry.get("id")
        assert entry["use"] in USES, entry["id"]
        assert entry["kind"] in KINDS, entry["id"]
        assert entry["file"] is None or FILE_NAME.match(entry["file"]), entry["id"]
        assert entry["title"].strip() and entry["expected"].strip(), entry["id"]
        assert entry["text"].strip() or entry["file"], f"{entry['id']}: neither text nor file"
        for text in sample_strings(entry):
            assert latin_only(text), f"{entry['id']}: a character outside Latin script"
            used = set(PLACEHOLDER.findall(text))
            assert used <= PLACEHOLDERS, f"{entry['id']}: unknown placeholder {sorted(used - PLACEHOLDERS)}"
            assert "{" not in PLACEHOLDER.sub("", text) and "}" not in PLACEHOLDER.sub("", text), entry["id"]
            for name in names:
                assert not re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.IGNORECASE), \
                    f"{entry['id']}: names '{name}' from the synthetic seed; use a placeholder"
            for index, name in enumerate(retailer, 1):  # never prints the name itself
                assert not re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.IGNORECASE), \
                    f"{entry['id']}: denylist entry {index}"
    by_id = {entry["id"]: entry for entry in entries}
    assert by_id["irrelevant"]["kind"] == by_id["injection"]["kind"] == "NONE"
    assert {by_id[key]["file"] for key in ("photo-only", "photo-and-count")} == {"milk-slot.jpg"}
    assert by_id["crate-expiry"]["file"] == "crates-expiry.jpg" and by_id["blurry-photo"]["file"] == "blurry.jpg"
    # the screenshot's reading is the picture's own words (07-CONTEXT D-34: $85.00 USD, handling included)
    assert "$85.00 USD, handling included" in by_id["quote-screenshot"]["expected"]
    assert "$125.00" in by_id["quote-pdf"]["expected"]


def test_the_rules_refuse_a_large_file_a_renamed_text_file_and_a_jpeg_with_exif(tmp_path):
    large = tmp_path / "large.png"
    large.write_bytes(PNG_SIGNATURE + b"\0" * (6 * 1024 * 1024))
    renamed = tmp_path / "renamed.jpg"
    renamed.write_text("not an image, only text named .jpg", encoding="utf-8")
    exif = tmp_path / "exif.jpg"
    app1 = b"Exif\0\0" + b"MM\0*\0\0\0\x08"
    exif.write_bytes(b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xda\0\x02\xff\xd9")
    clean = tmp_path / "clean.jpg"
    jfif = b"JFIF\0\x01\x01\0\0\x01\0\x01\0\0"
    clean.write_bytes(b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", len(jfif) + 2) + jfif + b"\xff\xda\0\x02\xff\xd9")

    assert any("over the" in problem for problem in asset_problems(large))
    assert any("not PNG, JPEG or PDF" in problem for problem in asset_problems(renamed))
    assert any("Exif" in problem for problem in asset_problems(exif))
    assert asset_problems(clean) == []
    assert [path.name for path in folder_assets(tmp_path) if asset_problems(path)] == \
        ["exif.jpg", "large.png", "renamed.jpg"]


def test_every_asset_passes_the_upload_rules():
    assert SAMPLES.is_file()
    problems = [problem for path in folder_assets(FOLDER) for problem in asset_problems(path)]
    assert problems == []


def test_required_assets_are_present():
    missing = [name for name in REQUIRED_ASSETS if not (FOLDER / name).is_file()]
    if os.environ.get("DEMO_SIGNALS_STRICT") == "1":
        assert missing == [], f"missing team images: {', '.join(missing)}"
    elif missing:
        pytest.skip(f"team images not placed yet: {', '.join(missing)} (set DEMO_SIGNALS_STRICT=1 to require them)")
