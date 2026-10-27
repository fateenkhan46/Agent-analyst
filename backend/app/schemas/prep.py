"""Contracts for Step 4: the Data Preparation Agent.

Design rules encoded here:

* A ``TransformationStep`` is **declarative** -- an operation name plus typed
  parameters drawn from a closed allowlist (``TransformOp``). The executor
  dispatches on the enum; it never evaluates generated code. This is what
  keeps LLM output out of the execution path.
* Every step carries ``risk`` and ``requires_approval``. Anything that can
  drop or merge rows defaults to requiring approval.
* ``RowCountDelta`` is recorded per step so lineage reconciles exactly:
  for any dataset, sum of deltas + source rows == final rows.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field, model_validator

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


class TransformOp(str, Enum):
    """Closed allowlist of executable operations.

    Adding a member requires a handler in ``app.services.prep.executor`` and a
    Power Query M emitter in ``app.services.prep.mquery``; the test
    ``test_every_op_has_handler_and_m_emitter`` enforces both.
    """

    RENAME_COLUMN = "rename_column"
    SELECT_COLUMNS = "select_columns"
    DROP_COLUMN = "drop_column"
    CAST_TYPE = "cast_type"
    PARSE_DATE = "parse_date"
    PARSE_NUMBER = "parse_number"
    PARSE_CURRENCY = "parse_currency"
    PARSE_PERCENTAGE = "parse_percentage"
    TRIM_WHITESPACE = "trim_whitespace"
    NORMALISE_CASE = "normalise_case"
    NORMALISE_IDENTIFIER = "normalise_identifier"
    MAP_CATEGORY = "map_category"
    FILL_MISSING = "fill_missing"
    FLAG_MISSING = "flag_missing"
    REMOVE_EMPTY_ROWS = "remove_empty_rows"
    DEDUPLICATE = "deduplicate"
    ADD_SURROGATE_KEY = "add_surrogate_key"
    ADD_COMPUTED_COLUMN = "add_computed_column"
    SPLIT_COLUMN = "split_column"
    UNPIVOT_COLUMNS = "unpivot_columns"
    FILTER_ROWS = "filter_rows"
    APPEND_TABLES = "append_tables"
    JOIN_TABLES = "join_tables"
    AGGREGATE = "aggregate"
    BUILD_DATE_TABLE = "build_date_table"
    BUILD_DIMENSION = "build_dimension"


#: Operations that can change the row count of a dataset. These default to
#: requires_approval=True and are always re-checked by QA.
ROW_AFFECTING_OPS = {
    TransformOp.REMOVE_EMPTY_ROWS,
    TransformOp.DEDUPLICATE,
    TransformOp.FILTER_ROWS,
    TransformOp.APPEND_TABLES,
    TransformOp.JOIN_TABLES,
    TransformOp.AGGREGATE,
    TransformOp.UNPIVOT_COLUMNS,
}

#: Operations that destroy information irrecoverably within the pipeline.
DESTRUCTIVE_OPS = {
    TransformOp.DROP_COLUMN,
    TransformOp.DEDUPLICATE,
    TransformOp.FILTER_ROWS,
    TransformOp.AGGREGATE,
}


class Risk(str, Enum):
    SAFE = "safe"              # lossless, reversible
    REVIEW = "review"          # changes values; inspect the preview
    DESTRUCTIVE = "destructive"  # can drop rows/columns


class ColumnMapping(Contract):
    """Source column -> canonical name. The semantic bridge across
    inconsistently-named sources."""

    id: str = Field(default_factory=lambda: new_id("map"))
    source_table: str
    source_column: str
    canonical_name: str
    canonical_type: CanonicalType
    confidence: Confidence = Confidence.MEDIUM
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)
    accepted: bool | None = None
    alternatives: list[str] = Field(default_factory=list)
    unit_conversion: str = Field(
        default="", description="e.g. 'minutes->hours (/60)'. Empty when none."
    )


class TransformationStep(Contract):
    id: str = Field(default_factory=lambda: new_id("step"))
    ordinal: int = 0
    op: TransformOp
    target_table: str
    params: dict[str, Any] = Field(default_factory=dict)

    title: str = ""
    reason: str = Field(default="", description="Why this step exists.")
    expected_effect: str = Field(default="", description="What it will do, in business terms.")
    risk: Risk = Risk.SAFE
    requires_approval: bool = False
    approved: bool | None = None
    evidence: list[str] = Field(default_factory=list)
    affects_columns: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.HIGH

    @model_validator(mode="after")
    def _escalate_risky_ops(self) -> "TransformationStep":
        if self.op in DESTRUCTIVE_OPS:
            self.risk = Risk.DESTRUCTIVE
            self.requires_approval = True
        elif self.op in ROW_AFFECTING_OPS and self.risk is Risk.SAFE:
            self.risk = Risk.REVIEW
        if self.confidence is Confidence.LOW:
            self.requires_approval = True
        return self


class JoinProposal(Contract):
    """A proposed join, backed by measured key overlap (not name similarity)."""

    id: str = Field(default_factory=lambda: new_id("join"))
    left_table: str
    left_keys: list[str]
    right_table: str
    right_keys: list[str]
    kind: str = Field(default="left", description="left | inner | full")
    implied_cardinality: str = "many_to_one"
    match_share: float = Field(default=0.0, ge=0.0, le=1.0)
    unmatched_left: int = 0
    unmatched_right: int = 0
    fan_out_risk: bool = Field(
        default=False,
        description="True when the right side is not unique on the join keys -- joining "
        "would duplicate left rows and inflate additive measures.",
    )
    recommended: bool = True
    recommendation_reason: str = ""
    confidence: Confidence = Confidence.MEDIUM
    evidence: list[str] = Field(default_factory=list)


class DuplicateFinding(Contract):
    """Duplicates are reported, never auto-removed: repeated rows are often
    legitimate (two timesheet lines for the same person/day)."""

    table: str
    keys: list[str]
    duplicate_group_count: int = 0
    affected_row_count: int = 0
    examples: list[dict[str, Any]] = Field(default_factory=list, max_length=5)
    likely_legitimate: bool = False
    assessment: str = ""


class DataQualityReport(Contract):
    """Produced twice -- before and after transformation -- so the effect of
    the plan is measurable rather than asserted."""

    id: str = Field(default_factory=lambda: new_id("dq"))
    project_id: str
    phase: str = Field(description="before | after")
    generated_at: datetime = Field(default_factory=utcnow)

    table_row_counts: dict[str, int] = Field(default_factory=dict)
    table_column_counts: dict[str, int] = Field(default_factory=dict)
    null_shares: dict[str, float] = Field(
        default_factory=dict, description="'table.column' -> share of null/blank."
    )
    type_conformance: dict[str, float] = Field(
        default_factory=dict, description="'table.column' -> share parseable as inferred type."
    )
    duplicate_row_counts: dict[str, int] = Field(default_factory=dict)
    orphan_key_counts: dict[str, int] = Field(
        default_factory=dict, description="'child.col->parent.col' -> unmatched distinct keys."
    )
    format_variant_counts: dict[str, int] = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list)

    @property
    def score(self) -> float:
        """0..1 completeness/conformance composite. Comparable across phases."""
        parts: list[float] = []
        if self.null_shares:
            parts.append(1.0 - sum(self.null_shares.values()) / len(self.null_shares))
        if self.type_conformance:
            parts.append(sum(self.type_conformance.values()) / len(self.type_conformance))
        total_rows = sum(self.table_row_counts.values())
        if total_rows:
            dup = sum(self.duplicate_row_counts.values())
            parts.append(max(0.0, 1.0 - dup / total_rows))
        return round(sum(parts) / len(parts), 4) if parts else 0.0


class RowCountDelta(Contract):
    step_id: str
    op: TransformOp
    table: str
    rows_before: int
    rows_after: int

    @property
    def delta(self) -> int:
        return self.rows_after - self.rows_before


class LineageEdge(Contract):
    """Column-level lineage: which source column(s) a prepared column came
    from, and through which steps."""

    target: str = Field(description="dataset.column")
    sources: list[str] = Field(default_factory=list, description="['table.column', ...]")
    via_steps: list[str] = Field(default_factory=list)
    transform_summary: str = ""


class TransformationPlan(Contract):
    """The Step-4 deliverable: a reviewable, approvable, executable plan."""

    id: str = Field(default_factory=lambda: new_id("plan"))
    project_id: str
    version: int = 1
    title: str = "Data preparation plan"
    summary: str = ""

    mappings: list[ColumnMapping] = Field(default_factory=list)
    steps: list[TransformationStep] = Field(default_factory=list)
    join_proposals: list[JoinProposal] = Field(default_factory=list)
    duplicate_findings: list[DuplicateFinding] = Field(default_factory=list)

    output_datasets: list[str] = Field(
        default_factory=list, description="Logical names of the datasets the plan produces."
    )
    provenance: Provenance
    generated_at: datetime = Field(default_factory=utcnow)
    approved: bool = False
    approved_at: datetime | None = None

    @property
    def steps_awaiting_approval(self) -> list[TransformationStep]:
        return [s for s in self.steps if s.requires_approval and s.approved is None]

    @property
    def rejected_steps(self) -> list[TransformationStep]:
        return [s for s in self.steps if s.approved is False]

    def ordered_steps(self) -> list[TransformationStep]:
        return sorted(self.steps, key=lambda s: s.ordinal)


class PreparedDataset(Contract):
    """A transformation output. Immutable once written; a new execution makes
    a new version."""

    id: str = Field(default_factory=lambda: new_id("ds"))
    project_id: str
    plan_id: str
    name: str
    version: int = 1
    stored_path: str = Field(description="Parquet/CSV key in object storage.")
    row_count: int = 0
    column_count: int = 0
    columns: list[dict[str, Any]] = Field(
        default_factory=list, description="[{name, canonical_type, nullable}]"
    )
    derived_from_tables: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    role_hint: str = Field(default="", description="fact | dimension | date | bridge")


class TransformationExecution(Contract):
    id: str = Field(default_factory=lambda: new_id("texec"))
    project_id: str
    plan_id: str
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    succeeded: bool = False
    skipped_steps: list[str] = Field(
        default_factory=list, description="Step ids skipped: unapproved or rejected."
    )
    failed_step_id: str = ""
    error: str = ""
    row_count_deltas: list[RowCountDelta] = Field(default_factory=list)
    lineage: list[LineageEdge] = Field(default_factory=list)
    datasets: list[PreparedDataset] = Field(default_factory=list)
    quality_before: DataQualityReport | None = None
    quality_after: DataQualityReport | None = None
    m_queries: dict[str, str] = Field(
        default_factory=dict, description="dataset name -> Power Query M script."
    )
    log: list[str] = Field(default_factory=list)


class PrepAgentInput(Contract):
    project_id: str
    profile_id: str
    tables: list[str] = Field(default_factory=list)
    requirements_id: str = ""
    target_kpis: list[str] = Field(default_factory=list)
    user_mapping_overrides: dict[str, str] = Field(
        default_factory=dict, description="'table.column' -> canonical name."
    )


class PrepAgentOutput(Contract):
    plan: TransformationPlan
    notes: list[str] = Field(default_factory=list)
