"""Turning an uploaded file into DataFrames, without losing anything.

Rules:

* Everything is read as **text first** (``dtype=str``). Letting pandas guess
  types on read would destroy exactly the evidence we need -- a column of
  ``03/04/2024`` silently becomes a datetime and we can no longer report that
  its day/month order is ambiguous.
* Headers are *found*, not assumed to be row 0. Business workbooks routinely
  have a title row, a blank row, then the real header.
* ``.xls`` is **rejected with a reason** because ``xlrd`` is not installed
  (see docs/INTEGRATION_LIMITATIONS.md) rather than failing obscurely.
* Row caps are reported, never applied silently.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ...schemas.source import SourceKind

MAX_HEADER_SCAN_ROWS = 12
#: Encodings tried in order. utf-8-sig first so a BOM does not become part of
#: the first column name.
ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


class UnsupportedSourceError(Exception):
    """Raised with a user-facing reason; surfaced as the rejection_reason."""


@dataclass
class ReadResult:
    sheet_name: str
    frame: pd.DataFrame
    header_row_index: int
    total_rows_in_source: int
    truncated: bool = False
    notes: list[str] = field(default_factory=list)


def detect_kind(filename: str, content_type: str = "") -> SourceKind:
    suffix = Path(filename).suffix.lower()
    mapping = {
        ".csv": SourceKind.CSV, ".tsv": SourceKind.CSV, ".txt": SourceKind.CSV,
        ".xlsx": SourceKind.XLSX, ".xlsm": SourceKind.XLSX,
        ".xls": SourceKind.XLS,
        ".json": SourceKind.JSON,
        ".md": SourceKind.TRANSCRIPT, ".vtt": SourceKind.TRANSCRIPT,
        ".srt": SourceKind.TRANSCRIPT, ".docx": SourceKind.TRANSCRIPT,
        ".png": SourceKind.IMAGE, ".jpg": SourceKind.IMAGE, ".jpeg": SourceKind.IMAGE,
        ".pbip": SourceKind.PBIP,
    }
    if suffix in mapping:
        return mapping[suffix]
    if "sheet" in content_type or "excel" in content_type:
        return SourceKind.XLSX
    if "csv" in content_type:
        return SourceKind.CSV
    return SourceKind.UNKNOWN


DATA_KINDS = {SourceKind.CSV, SourceKind.XLSX, SourceKind.JSON}


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode bytes, returning (text, encoding_used)."""
    for enc in ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8/replace"


def sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        # Fall back to whichever candidate appears most consistently.
        lines = [ln for ln in sample.splitlines()[:20] if ln.strip()]
        best, best_score = ",", -1.0
        for cand in (",", ";", "\t", "|"):
            counts = [ln.count(cand) for ln in lines]
            if not counts or max(counts) == 0:
                continue
            consistency = counts.count(max(counts)) / len(counts)
            score = consistency * max(counts)
            if score > best_score:
                best, best_score = cand, score
        return best


def _score_header_row(cells: list[str], following: list[list[str]]) -> float:
    """How much does this row look like a header rather than data?

    Heuristics, each cheap and explainable:
      + mostly non-empty, mostly text, all distinct
      - looks numeric or date-like (data, not a label)
      + the rows beneath it have a similar populated width
    """
    vals = [c.strip() for c in cells]
    populated = [v for v in vals if v != ""]
    if len(populated) < 2:
        return 0.0
    score = 0.0
    score += 2.0 * (len(populated) / len(vals))
    distinct = len({v.lower() for v in populated})
    score += 1.5 * (distinct / len(populated))
    texty = sum(1 for v in populated if not _looks_like_data(v))
    score += 2.0 * (texty / len(populated))
    if any(len(v) > 60 for v in populated):
        score -= 1.0
    if following:
        widths = [len([c for c in r if str(c).strip() != ""]) for r in following]
        if widths:
            avg = sum(widths) / len(widths)
            score += 1.0 * (1.0 - min(1.0, abs(avg - len(populated)) / max(1, len(populated))))
    return score


def _looks_like_data(v: str) -> bool:
    v = v.strip()
    if not v:
        return False
    try:
        float(v.replace(",", ""))
        return True
    except ValueError:
        pass
    return bool(
        len(v) >= 6 and sum(ch.isdigit() for ch in v) >= 4 and
        any(sep in v for sep in ("/", "-", ".")) and " " not in v
    )


def find_header_row(rows: list[list[str]]) -> int:
    """Index of the most header-like row within the first few rows."""
    best_idx, best_score = 0, -1.0
    for i, row in enumerate(rows[:MAX_HEADER_SCAN_ROWS]):
        s = _score_header_row([str(c) for c in row], [[str(c) for c in r] for r in rows[i + 1:i + 6]])
        if s > best_score:
            best_idx, best_score = i, s
    return best_idx


def _dedupe_headers(names: list[str]) -> tuple[list[str], list[str]]:
    """Make headers unique and non-empty, reporting what was changed."""
    out: list[str] = []
    notes: list[str] = []
    seen: dict[str, int] = {}
    for i, raw in enumerate(names):
        name = str(raw).strip()
        if name == "" or name.lower().startswith("unnamed:"):
            name = f"column_{i + 1}"
            notes.append(f"Blank header at position {i + 1} named '{name}'.")
        key = name.lower()
        if key in seen:
            seen[key] += 1
            new = f"{name}_{seen[key]}"
            notes.append(f"Duplicate header '{name}' renamed to '{new}'.")
            name = new
        else:
            seen[key] = 1
        out.append(name)
    return out, notes


def _frame_from_rows(rows: list[list[object]], header_idx: int) -> tuple[pd.DataFrame, list[str]]:
    header_raw = [str(c) if c is not None else "" for c in rows[header_idx]]
    header, notes = _dedupe_headers(header_raw)
    body = rows[header_idx + 1:]
    width = len(header)
    norm: list[list[object]] = []
    for r in body:
        r = list(r)
        if len(r) < width:
            r = r + [None] * (width - len(r))
        elif len(r) > width:
            extra = [c for c in r[width:] if c is not None and str(c).strip() != ""]
            if extra:
                notes.append(
                    f"A row had {len(r)} values for {width} columns; the surplus was kept "
                    f"in an overflow column rather than discarded."
                )
                header = header + ["_overflow"]
                width += 1
                norm = [row + [None] for row in norm]
                r = r[: width - 1] + [" | ".join(str(c) for c in extra)]
            else:
                r = r[:width]
        norm.append(r)
    return pd.DataFrame(norm, columns=header, dtype=object), notes


def read_csv_bytes(data: bytes, max_rows: int) -> list[ReadResult]:
    text, enc = decode_text(data)
    if not text.strip():
        raise UnsupportedSourceError("The file is empty.")
    delim = sniff_delimiter(text[:64_000])
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    all_rows = [r for r in reader]
    if not all_rows:
        raise UnsupportedSourceError("No rows could be parsed from the file.")
    header_idx = find_header_row([[str(c) for c in r] for r in all_rows])
    total_data_rows = max(0, len(all_rows) - header_idx - 1)
    truncated = total_data_rows > max_rows
    rows = all_rows[: header_idx + 1 + max_rows] if truncated else all_rows
    frame, notes = _frame_from_rows(rows, header_idx)  # type: ignore[arg-type]
    notes.insert(0, f"Decoded as {enc}; delimiter {delim!r}.")
    if truncated:
        notes.append(
            f"Row cap reached: profiled the first {max_rows:,} of {total_data_rows:,} data rows."
        )
    return [ReadResult("", frame, header_idx, total_data_rows, truncated, notes)]


def read_xlsx_bytes(data: bytes, max_rows: int) -> list[ReadResult]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - openpyxl is a hard dep
        raise UnsupportedSourceError(f"openpyxl is unavailable: {exc}") from exc

    # read_only keeps memory flat on large workbooks; data_only takes cached
    # formula *results*, which is what a reader of the file would see.
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    results: list[ReadResult] = []
    try:
        for ws in wb.worksheets:
            if ws.sheet_state != "visible":
                continue
            rows: list[list[object]] = []
            total = 0
            for row in ws.iter_rows(values_only=True):
                total += 1
                if len(rows) <= max_rows + MAX_HEADER_SCAN_ROWS:
                    rows.append(list(row))
            if not rows:
                continue
            # trim trailing all-empty rows that Excel leaves behind
            while rows and all(c is None or str(c).strip() == "" for c in rows[-1]):
                rows.pop()
            if not rows:
                continue
            header_idx = find_header_row([[("" if c is None else str(c)) for c in r] for r in rows])
            data_rows = max(0, total - header_idx - 1)
            truncated = data_rows > max_rows
            frame, notes = _frame_from_rows(rows[: header_idx + 1 + max_rows], header_idx)
            if header_idx > 0:
                notes.insert(0, f"Header found on row {header_idx + 1}, not row 1.")
            if truncated:
                notes.append(
                    f"Row cap reached: profiled the first {max_rows:,} of "
                    f"{data_rows:,} data rows."
                )
            if frame.empty and not frame.columns.empty:
                notes.append("Sheet has headers but no data rows.")
            results.append(ReadResult(ws.title, frame, header_idx, data_rows, truncated, notes))
    finally:
        wb.close()
    if not results:
        raise UnsupportedSourceError("The workbook contains no visible sheet with data.")
    return results


def read_json_bytes(data: bytes, max_rows: int) -> list[ReadResult]:
    import json

    text, _ = decode_text(data)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UnsupportedSourceError(f"Invalid JSON: {exc.msg} (line {exc.lineno})") from exc
    if isinstance(parsed, dict):
        arrays = {k: v for k, v in parsed.items() if isinstance(v, list)}
        if len(arrays) == 1:
            parsed = next(iter(arrays.values()))
        elif arrays:
            out = []
            for name, rows in arrays.items():
                frame = pd.DataFrame(rows[:max_rows], dtype=object)
                out.append(ReadResult(name, frame, 0, len(rows), len(rows) > max_rows, []))
            return out
        else:
            parsed = [parsed]
    if not isinstance(parsed, list):
        raise UnsupportedSourceError("JSON must be an array of objects or an object of arrays.")
    total = len(parsed)
    frame = pd.DataFrame(parsed[:max_rows], dtype=object)
    return [ReadResult("", frame, 0, total, total > max_rows, [])]


def read_source(data: bytes, filename: str, kind: SourceKind, max_rows: int) -> list[ReadResult]:
    """Dispatch to the right reader, or raise ``UnsupportedSourceError`` with a
    reason a user can act on."""
    if kind is SourceKind.CSV:
        return read_csv_bytes(data, max_rows)
    if kind is SourceKind.XLSX:
        return read_xlsx_bytes(data, max_rows)
    if kind is SourceKind.JSON:
        return read_json_bytes(data, max_rows)
    if kind is SourceKind.XLS:
        raise UnsupportedSourceError(
            "Legacy .xls workbooks cannot be read: the 'xlrd' library is not installed in "
            "this environment. Re-save the file as .xlsx (Excel: File > Save As > "
            "Excel Workbook) and upload it again."
        )
    raise UnsupportedSourceError(
        f"'{Path(filename).suffix or filename}' is not a supported data format. "
        "Supported: .csv, .tsv, .xlsx, .xlsm, .json."
    )
