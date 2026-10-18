"""D-11 (05-15): the browser suite serves the release reference set. The served dataset_version equals the one
tests/api/regression.json was recorded on (read, never edited), and Phase 3's release gate passes on the committed
set, run read-only with no database and no cloud (as 05-15 ran it)."""
from __future__ import annotations

import json
import pathlib

import pytest

from data import reference
from tests.web.conftest import case_id_by_tag, case_json, session_json

pytestmark = pytest.mark.web

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_the_suite_serves_the_release_set(visitor):
    page = visitor.page
    release = json.loads((ROOT / "tests" / "api" / "regression.json").read_text(encoding="utf-8"))["dataset_version"]
    served = case_json(page, case_id_by_tag(page, "milk-story"))["versions"]["dataset_version"]
    assert served == release, (served, release)
    gate = reference.release_check(reference.load())
    assert gate["dataset_version"] == release, (gate["dataset_version"], release)
    assert session_json(page)["label"] == "Synthetic data · hypothetical scenario"
