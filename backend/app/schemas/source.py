"""Contracts for uploaded sources and their measured profiles.

Invariant: a ``SourceFile`` record and its bytes in object storage are
**immutable**. Transformations never write back to a source; they produce new
``dataset`` artifacts with explicit lineage. This is what lets QA reconcile
transformed row counts against the originals.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field, field_validator

from .common import (
    CanonicalType,
    Confidence,
    Contract,
    Issue,
    Provenance,
    Severity,
    new_id,
    utcnow,
)


class SourceKind(str, Enum):
    CSV = "csv"
    XLSX = "xlsx"
    XLS = "xls"
    JSON = "json"
    TRANSCRIPT = "transcript"
    BRIEF = "brief"
    IMAGE = "image"
    PBIP = "pbip"
    UNKNOWN = "unknown"


class IngestStatus(str, Enum):
    UPLOADED = "uploaded"
    PROFILING = "profiling"
    PROFILED = "profiled"
    REJECTED = "rejected"
    FAILED = "failed"


class SourceFile(Contract):
    id: str = Field(default_factory=lambda: new_id("src"))
    project_id: str
    original_filename: str = Field(description="Never altered; shown in lineage.")
    stored_path: str = Field(description="Object-storage key of the immutable upload.")
    kind: SourceKind
    content_type: str = ""
    size_bytes: int = 0
    sha256: str = ""
    status: IngestStatus = IngestStatus.UPLOADED
    rejection_reason: str = ""
    uploaded_at: datetime = Field(default_factory=utcnow)
    uploaded_by: str = ""


# --------------------------------------------------------------------------
# Column-level profile
# --------------------------------------------------------------------------
class FormatVariant(Contract):
    """One concrete surface form found in a column, with how often it occurs.

    This is the raw material for 'inconsistent date/currency format'
    detection -- we report the actual competing patterns and their counts
    rather than a vague warning.
    """

    pattern: str = Field(description="e.g. '%d/%m/%Y', '1.234,56', '(1,234)', 'EUR 1234'")
    description: str = ""
    count: int = 0
    examples: list[str] = Field(default_factory=list, max_length=5)


class NumericStats(Contract):
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    median: float | None = None
    stddev: float | None = None
    p01: float | None = None
    p25: float | None = None
    p75: float | None = None
    p99: float | None = None
    negative_count: int = 0
    zero_count: int = 0


class TemporalStats(Contract):
    min_date: str | None = None
    max_date: str | None = None
    distinct_days: int = 0
    future_dated_count: int = 0
    granularity: str = Field(default="", description="day | month | quarter | year | mixed")


class Outlier(Contract):
    row_index: int
    value: str
    reason: str
    z_score: float | None = None


class ColumnProfile(Contract):
    """Measured facts about one column. Every field here is computed by code,
    never by an LLM -- see ``Provenance.origin == DETERMINISTIC``."""

    id: str = Field(default_factory=lambda: new_id("col"))
    source_table_id: str
    name: str = Field(description="The column header exactly as it appears in the source.")
    ordinal: int = 0

    inferred_type: CanonicalType = CanonicalType.STRING
    type_confidence: Confidence = Confidence.MEDIUM
    raw_pandas_dtype: str = ""

    row_count: int = 0
    null_count: int = 0
    blank_count: int = Field(default=0, description="Empty / whitespace-only strings.")
    unique_count: int = 0
    duplicate_count: int = Field(
        default=0, description="row_count - null_count - unique_count; repeated values."
    )

    sample_values: list[str] = Field(default_factory=list, max_length=12)
    top_values: list[dict[str, Any]] = Field(
        default_factory=list, description="[{value, count, share}] for the 10 commonest."
    )

    format_variants: list[FormatVariant] = Field(default_factory=list)
    numeric: NumericStats | None = None
    temporal: TemporalStats | None = None
    outliers: list[Outlier] = Field(default_factory=list, max_length=25)

    # key candidacy -- evidence based, not name based
    is_unique: bool = False
    is_complete: bool = Field(default=False, description="No nulls or blanks.")
    primary_key_score: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="Evidence score: uniqueness x completeness x type plausibility.",
    )
    detected_currency_symbols: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)

    @property
    def null_share(self) -> float:
        return (self.null_count + self.blank_count) / self.row_count if self.row_count else 0.0

    @property
    def cardinality_ratio(self) -> float:
        populated = self.row_count - self.null_count - self.blank_count
        return self.unique_count / populated if populated else 0.0


class SourceTable(Contract):
    """One sheet of a workbook, or one CSV. The unit the profiler works on."""

    id: str = Field(default_factory=lambda: new_id("tbl"))
    project_id: str
    source_file_id: str
    sheet_name: str = Field(default="", description="Empty for CSV.")
    table_name: str = Field(description="Slugged, unique-within-project logical name.")
    display_name: str = ""

    row_count: int = 0
    column_count: int = 0
    header_row_index: int = Field(default=0, description="0-based row where headers were found.")
    preview_rows: list[dict[str, Any]] = Field(default_factory=list, max_length=50)

    columns: list[ColumnProfile] = Field(default_factory=list)

    duplicate_row_count: int = 0
    fully_empty_row_count: int = 0
    naming_convention: str = Field(
        default="", description="snake_case | camelCase | PascalCase | 'Title Case' | mixed"
    )
    candidate_primary_keys: list[list[str]] = Field(
        default_factory=list,
        description="Single- and composite-key candidates, best first.",
    )
    issues: list[Issue] = Field(default_factory=list)
    profiled_at: datetime | None = None

    def column(self, name: str) -> ColumnProfile | None:
        lowered = name.strip().lower()
        for c in self.columns:
            if c.name.strip().lower() == lowered:
                return c
        return None

    @field_validator("table_name")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("table_name must not be empty")
        return v


# --------------------------------------------------------------------------
# Cross-table findings
# --------------------------------------------------------------------------
class KeyOverlap(Contract):
    """Measured referential overlap between two columns. The sole evidence
    the Semantic Model Agent is allowed to propose a relationship from --
    column-name similarity alone is never sufficient."""

    left: str = Field(description="table.column")
    right: str = Field(description="table.column")
    left_distinct: int = 0
    right_distinct: int = 0
    matched_distinct: int = 0
    left_match_share: float = Field(default=0.0, ge=0.0, le=1.0)
    right_match_share: float = Field(default=0.0, ge=0.0, le=1.0)
    left_is_unique: bool = False
    right_is_unique: bool = False
    unmatched_left_examples: list[str] = Field(default_factory=list, max_length=10)
    unmatched_right_examples: list[str] = Field(default_factory=list, max_length=10)
    name_similarity: float = Field(default=0.0, ge=0.0, le=1.0)
    type_compatible: bool = True

    @property
    def implied_cardinality(self) -> str:
        if self.left_is_unique and self.right_is_unique:
            return "one_to_one"
        if self.right_is_unique:
            return "many_to_one"
        if self.left_is_unique:
            return "one_to_many"
        return "many_to_many"


class SourceDataProfile(Contract):
    """The Step-2 deliverable: everything measured about the uploaded data."""

    id: str = Field(default_factory=lambda: new_id("prof"))
    project_id: str
    generated_at: datetime = Field(default_factory=utcnow)
    tables: list[SourceTable] = Field(default_factory=list)
    key_overlaps: list[KeyOverlap] = Field(default_factory=list)
    cross_table_issues: list[Issue] = Field(default_factory=list)
    provenance: Provenance

    @property
    def total_rows(self) -> int:
        return sum(t.row_count for t in self.tables)

    def issue_counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Severity}
        for t in self.tables:
            for i in t.issues:
                counts[i.severity.value] += 1
            for c in t.columns:
                for i in c.issues:
                    counts[i.severity.value] += 1
        for i in self.cross_table_issues:
            counts[i.severity.value] += 1
        return counts
