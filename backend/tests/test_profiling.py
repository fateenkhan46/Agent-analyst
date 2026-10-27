"""Profiling: the defects the brief names, each asserted explicitly."""
from __future__ import annotations

import pytest

from app.schemas.common import CanonicalType
from app.services.profiling.inference import (
    canonicalise_name, classify_column, classify_value, name_similarity, naming_convention,
)
from app.services.profiling.profiler import _to_float, profile_table


# --- value classification ------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        ("2024-03-15", CanonicalType.DATE),
        ("15/03/2024", CanonicalType.DATE),
        ("15-Mar-2024", CanonicalType.DATE),
        ("March 15, 2024", CanonicalType.DATE),
        ("Q1-2024", CanonicalType.DATE),
        ("$1,234.56", CanonicalType.CURRENCY),
        ("EUR 1234", CanonicalType.CURRENCY),
        ("1.234,56", CanonicalType.DECIMAL),
        ("(1,234.56)", CanonicalType.DECIMAL),
        ("12.5%", CanonicalType.PERCENTAGE),
        ("7.5h", CanonicalType.DURATION),
        ("07:30", CanonicalType.DURATION),
        ("PRJ-1001", CanonicalType.IDENTIFIER),
        ("E1", CanonicalType.IDENTIFIER),
        ("true", CanonicalType.BOOLEAN),
        ("some free text", CanonicalType.STRING),
    ],
)
def test_value_classification(value: str, expected: CanonicalType) -> None:
    got = classify_value(value)
    assert got is not None and got.canonical is expected, f"{value!r} -> {got}"


def test_business_status_words_are_not_booleans() -> None:
    """'Active'/'Complete' are status values; treating them as booleans would
    hide them from category and key analysis."""
    for word in ("Active", "Closed", "Complete", "Completed", "Inactive"):
        got = classify_value(word)
        assert got is not None and got.canonical is not CanonicalType.BOOLEAN, word


def test_ambiguous_date_order_is_flagged_not_guessed() -> None:
    got = classify_value("03/04/2024")
    assert got is not None and got.ambiguous, "dd/mm vs mm/dd must be reported as ambiguous"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$1,234.56", 1234.56), ("(1,234.56)", -1234.56), ("1.234,56", 1234.56),
        ("1 234,56", 1234.56), ("12.5%", 0.125), ("7.5h", 7.5),
        ("450min", 7.5), ("07:30", 7.5), ("1234-", -1234.0), ("abc", None), ("", None),
    ],
)
def test_number_normalisation(text: str, expected: float | None) -> None:
    assert _to_float(text) == expected


def test_mixed_date_formats_are_counted_not_collapsed() -> None:
    v = classify_column(
        ["2024-03-15", "15/03/2024", "March 15, 2024", "2024-03-16"], "start_date"
    )
    assert v.canonical is CanonicalType.DATE
    assert len(v.pattern_counts) == 3, dict(v.pattern_counts)


def test_naming_convention_detection() -> None:
    assert naming_convention(["project_id", "cost_amount"]) == "snake_case"
    assert naming_convention(["ProjectId", "CostAmount"]) == "PascalCase"
    assert naming_convention(["Project Code", "Project Name"]) == "Title Case"
    assert naming_convention(["ProjectID", "project_name", "Cost Amount"]) == "mixed"


def test_name_canonicalisation_bridges_systems() -> None:
    assert canonicalise_name("Proj ID") == canonicalise_name("project_id")
    assert name_similarity("Project ID", "proj_id") == 1.0
    assert name_similarity("Cost", "Employee Name") == 0.0


# --- table profiling -----------------------------------------------------
def _frame(rows: list[dict]) -> object:
    import pandas as pd
    return pd.DataFrame(rows, dtype=object)


def test_near_unique_key_with_duplicates_is_flagged() -> None:
    """The conflicting-record case: a key column with a few repeats."""
    rows = [{"timesheet_id": f"TS-{i}", "hours": "8"} for i in range(30)]
    rows.append({"timesheet_id": "TS-0", "hours": "9"})  # same key, different value
    t = profile_table(_frame(rows), project_id="p", source_file_id="f", table_name="labour")
    codes = {i.code for c in t.columns for i in c.issues}
    assert "column.duplicate_key_values" in codes, codes


def test_multiple_currencies_in_one_column_is_critical() -> None:
    rows = [{"amount": "$100.00"}, {"amount": "EUR 200.00"}, {"amount": "$300.00"}]
    t = profile_table(_frame(rows), project_id="p", source_file_id="f", table_name="c")
    issue = next(
        (i for c in t.columns for i in c.issues if i.code == "column.multiple_currencies"), None
    )
    assert issue is not None, [i.code for c in t.columns for i in c.issues]
    assert issue.severity.value == "critical"


def test_duplicate_rows_are_reported_not_removed() -> None:
    rows = [{"a": "1", "b": "x"}, {"a": "1", "b": "x"}, {"a": "2", "b": "y"}]
    t = profile_table(_frame(rows), project_id="p", source_file_id="f", table_name="t")
    assert t.row_count == 3, "source rows must never be dropped during profiling"
    assert t.duplicate_row_count == 1
    assert any(i.code == "table.duplicate_rows" for i in t.issues)


def test_composite_primary_key_detection() -> None:
    rows = [
        {"order": "O1", "line": "1", "qty": "5"},
        {"order": "O1", "line": "2", "qty": "3"},
        {"order": "O2", "line": "1", "qty": "7"},
    ]
    t = profile_table(_frame(rows), project_id="p", source_file_id="f", table_name="lines")
    assert ["order", "line"] in t.candidate_primary_keys, t.candidate_primary_keys


def test_no_primary_key_is_reported() -> None:
    rows = [{"cat": "a", "v": "1"}, {"cat": "a", "v": "1"}, {"cat": "b", "v": "2"}]
    t = profile_table(_frame(rows), project_id="p", source_file_id="f", table_name="t")
    assert any(i.code == "table.no_primary_key" for i in t.issues)


def test_near_unique_column_never_scores_a_perfect_key() -> None:
    rows = [{"invoice": f"INV-{i}"} for i in range(50)]
    rows.append({"invoice": "INV-0"})
    t = profile_table(_frame(rows), project_id="p", source_file_id="f", table_name="t")
    col = t.column("invoice")
    assert col is not None and not col.is_unique
    assert col.primary_key_score < 1.0, "a non-unique column must not read as a perfect key"


def test_empty_column_is_reported() -> None:
    rows = [{"a": "1", "unused": ""}, {"a": "2", "unused": None}]
    t = profile_table(_frame(rows), project_id="p", source_file_id="f", table_name="t")
    col = t.column("unused")
    assert col is not None and col.inferred_type is CanonicalType.EMPTY
    assert any(i.code == "column.entirely_empty" for i in col.issues)
