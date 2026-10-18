"""The assumptions table is generated from reference/ and regenerates byte-identical (DATA-06).

Expected paths are written from DATA-06 and the 03-13 plan, never pasted from generator output.
"""
import json

from data import assumptions as A
from data import config as C

TABLE = A.OUT / A.NAME


def test_the_table_regenerates_byte_identical():
    committed = TABLE.read_bytes()
    assert A.render(A.rows(), C.fingerprint()).encode("utf-8") == committed


def test_the_table_carries_the_synthetic_label_and_the_reference_fingerprint():
    text = TABLE.read_text(encoding="utf-8")
    assert "Synthetic data · hypothetical scenario" in text.splitlines()
    assert f"(fingerprint {C.fingerprint()})" in text
    assert "Never edit by hand." in text
    assert text.endswith("|\n") and not text.endswith("\n\n")


def test_rows_are_sorted_by_path_and_each_has_a_type_and_source():
    rows = A.rows()
    paths = [row["path"] for row in rows]
    assert paths == sorted(paths)
    assert len(paths) == len(set(paths))
    for row in rows:
        assert set(row) == {"path", "value", "unit", "type", "source", "note"}
        assert row["type"] in C.TYPES, row["path"]
        assert row["source"].strip(), row["path"]
        assert row["path"].split(".")[0] in ("policy", "world", "scenarios")


def test_the_table_has_one_row_per_public_leaf():
    expected = sum(len(C.leaves(C.read(C.REF_IN / f"{name}.json"), name)) for name in ("policy", "world", "scenarios"))
    assert len(A.rows()) == expected
    table_rows = [line for line in TABLE.read_text(encoding="utf-8").splitlines() if line.startswith("| ")]
    assert len(table_rows) == expected + 1  # the header row


def test_the_hidden_response_is_never_in_the_table():
    rows = A.rows()
    assert not any(row["path"].startswith("hidden") for row in rows)
    assert not any("markdown_truth" in json.dumps(row, ensure_ascii=False) for row in rows)
    assert "markdown_truth" not in TABLE.read_text(encoding="utf-8")


def test_the_data06_rows_are_in_the_table():
    paths = {row["path"] for row in A.rows()}
    for path in ("policy.markdown_response.m20", "policy.markdown_response.m30",
                 "policy.engine_policy.waste_price", "policy.engine_policy.shortlist_step"):
        assert path in paths, path
    for suffix in (".fixed_cost", ".cancel_cost", ".disposal_cost"):
        assert any(p.endswith(suffix) for p in paths), suffix
    assert any(p.startswith("world.ordering.delivery_multiplier") for p in paths)


def test_the_selling_window_and_order_case_rows_are_labelled_assumptions():
    rows = A.rows()
    for suffix in (".selling_window", ".order_case"):
        matching = [row for row in rows if row["path"].endswith(suffix)]
        assert matching, suffix
        assert all(row["type"] == "Assumption" for row in matching), suffix


def test_the_table_keeps_the_middle_dot():
    assert "·" in TABLE.read_text(encoding="utf-8")


def test_a_pipe_in_a_cell_is_escaped():
    row = {"path": "policy.x", "value": "a|b", "unit": "", "type": "Data", "source": "s|t", "note": ""}
    line = A.render([row], "ref-000000000000").splitlines()[-1]
    assert line == '| policy.x | "a\\|b" |  | Data | s\\|t |  |'


def test_write_creates_the_directory_and_matches_render(tmp_path):
    out = A.write(tmp_path / "nested" / "reference")
    assert out.name == "assumptions.md"
    assert out.read_bytes() == TABLE.read_bytes()
