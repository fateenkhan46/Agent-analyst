"""Readers: messy real-world files must be read without losing data."""
from __future__ import annotations

import pytest

from app.schemas.source import SourceKind
from app.services.profiling.readers import (
    UnsupportedSourceError, find_header_row, read_csv_bytes, read_source, read_xlsx_bytes,
    sniff_delimiter,
)
from tests.conftest import make_xlsx


def test_header_row_found_below_a_title_row() -> None:
    data = b"Quarterly Export\n\nProject ID,Cost\nPRJ-1,100\nPRJ-2,200\n"
    res = read_csv_bytes(data, 1000)[0]
    assert res.header_row_index == 2
    assert list(res.frame.columns) == ["Project ID", "Cost"]
    assert len(res.frame) == 2


def test_duplicate_headers_are_renamed_and_reported() -> None:
    res = read_csv_bytes(b"Cost,Cost,Notes\n1,2,x\n", 1000)[0]
    assert list(res.frame.columns) == ["Cost", "Cost_2", "Notes"]
    assert any("Duplicate header" in n for n in res.notes)


def test_blank_headers_are_named_and_reported() -> None:
    res = read_csv_bytes(b"A,,C\n1,2,3\n", 1000)[0]
    assert "column_2" in list(res.frame.columns)
    assert any("Blank header" in n for n in res.notes)


def test_surplus_values_are_kept_not_discarded() -> None:
    """A ragged row must not silently lose its extra values."""
    res = read_csv_bytes(b"A,B\n1,2\n3,4,SURPLUS\n", 1000)[0]
    assert "_overflow" in list(res.frame.columns)
    assert "SURPLUS" in res.frame["_overflow"].tolist()
    assert any("surplus" in n for n in res.notes)


def test_row_cap_is_reported_not_silent() -> None:
    data = b"a,b\n" + b"1,2\n" * 50
    res = read_csv_bytes(data, 10)[0]
    assert res.truncated and res.total_rows_in_source == 50 and len(res.frame) == 10
    assert any("Row cap reached" in n for n in res.notes)


@pytest.mark.parametrize("delim", [",", ";", "\t", "|"])
def test_delimiter_sniffing(delim: str) -> None:
    sample = delim.join(["a", "b", "c"]) + "\n" + delim.join(["1", "2", "3"]) + "\n"
    assert sniff_delimiter(sample) == delim


def test_multiple_worksheets_are_each_profiled() -> None:
    data = make_xlsx({
        "Sheet A": [["x", "y"], [1, 2]],
        "Sheet B": [["p", "q"], [3, 4]],
    })
    results = read_xlsx_bytes(data, 1000)
    assert {r.sheet_name for r in results} == {"Sheet A", "Sheet B"}


def test_hidden_sheets_are_skipped() -> None:
    import io
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.title = "Visible"
    wb["Visible"].append(["a"])
    wb["Visible"].append([1])
    h = wb.create_sheet("Hidden")
    h.sheet_state = "hidden"
    h.append(["secret"])
    buf = io.BytesIO()
    wb.save(buf)
    results = read_xlsx_bytes(buf.getvalue(), 1000)
    assert [r.sheet_name for r in results] == ["Visible"]


def test_legacy_xls_is_refused_with_actionable_guidance() -> None:
    with pytest.raises(UnsupportedSourceError) as exc:
        read_source(b"\xd0\xcf\x11\xe0", "old.xls", SourceKind.XLS, 100)
    message = str(exc.value)
    assert "xlrd" in message and ".xlsx" in message


def test_unsupported_format_names_what_is_supported() -> None:
    with pytest.raises(UnsupportedSourceError) as exc:
        read_source(b"%PDF-1.4", "a.pdf", SourceKind.UNKNOWN, 100)
    assert ".csv" in str(exc.value) and ".xlsx" in str(exc.value)


def test_empty_file_is_refused() -> None:
    with pytest.raises(UnsupportedSourceError):
        read_csv_bytes(b"   \n", 100)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp1252"])
def test_encodings_round_trip(encoding: str) -> None:
    text = "name,city\nJosé,Köln\n"
    res = read_csv_bytes(text.encode(encoding), 100)[0]
    assert res.frame["name"].tolist() == ["José"]
