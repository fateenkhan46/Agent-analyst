"""Contracts for Steps 5 and 6: the semantic model and its DAX measures.

These models are the single source of truth for TMDL emission
(``app.services.pbip``), DAX reference validation
(``app.services.dax.references``) and DuckDB evaluation
(``app.services.dax.evaluator``). Keeping one shape means a measure cannot
validate against one view of the model and be written against another.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field, model_validator

from .common import (
    POWERBI_DATA_TYPE,
    CanonicalType,
    Confidence,
    Contract,
    Issue,
    Provenance,
    Severity,
    new_id,
    utcnow,
)


class TableRole(str, Enum):
    FACT = "fact"
    DIMENSION = "dimension"
    DATE = "date"
    BRIDGE = "bridge"
    AGGREGATE = "aggregate"
    HELPER = "helper"


class Cardinality(str, Enum):
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


class CrossFilter(str, Enum):
    SINGLE = "single"
    BOTH = "both"
    NONE = "none"


class SummarizeBy(str, Enum):
    NONE = "none"
    SUM = "sum"
    COUNT = "count"
    MIN = "min"
    MAX = "max"
    AVERAGE = "average"
    DISTINCT_COUNT = "distinctCount"


class ColumnDefinition(Contract):
    name: str
    source_column: str = Field(default="", description="Name in the prepared dataset.")
    data_type: CanonicalType = CanonicalType.STRING
    is_key: bool = False
    is_hidden: bool = False
    is_nullable: bool = True
    format_string: str = ""
    description: str = ""
    summarize_by: SummarizeBy = Field(
        default=SummarizeBy.NONE,
        description="Defaults to 'none' for keys and for every column, so implicit "
        "measures cannot silently sum an id or a rate.",
    )
    sort_by_column: str = ""
    data_category: str = Field(default="", description="e.g. 'Years','Months' for date tables.")

    @property
    def powerbi_type(self) -> str:
        return POWERBI_DATA_TYPE.get(self.data_type, "string")


class TableDefinition(Contract):
    name: str
    role: TableRole
    dataset_name: str = Field(default="", description="PreparedDataset this table loads from.")
    description: str = ""
    columns: list[ColumnDefinition] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    row_count: int = 0
    grain: str = Field(default="", description="One row per ... (stated explicitly).")
    is_hidden: bool = False
    m_expression: str = Field(default="", description="Power Query M partition source.")
    rationale: str = ""
    confidence: Confidence = Confidence.MEDIUM

    def column(self, name: str) -> ColumnDefinition | None:
        lowered = name.strip().lower()
        return next((c for c in self.columns if c.name.strip().lower() == lowered), None)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


class RelationshipDefinition(Contract):
    """Power BI relationships are always physically many-to-one from the fact
    side. We store the from/to in that direction and validate it."""

    id: str = Field(default_factory=lambda: new_id("rel"))
    from_table: str = Field(description="Many side (usually the fact).")
    from_column: str
    to_table: str = Field(description="One side (usually the dimension).")
    to_column: str
    cardinality: Cardinality = Cardinality.MANY_TO_ONE
    cross_filter: CrossFilter = CrossFilter.SINGLE
    is_active: bool = True
    rely_on_referential_integrity: bool = False

    match_share: float = Field(default=0.0, ge=0.0, le=1.0)
    orphan_key_count: int = 0
    to_side_is_unique: bool = True
    rationale: str = ""
    evidence: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.MEDIUM
    requires_approval: bool = False
    approved: bool | None = None

    @model_validator(mode="after")
    def _gate_uncertain_relationships(self) -> "RelationshipDefinition":
        # A relationship we are not confident about must never be applied
        # silently just because the column names look alike.
        if self.confidence is not Confidence.HIGH or not self.to_side_is_unique:
            self.requires_approval = True
        if self.cardinality is Cardinality.MANY_TO_MANY:
            self.requires_approval = True
        return self

    @property
    def key(self) -> str:
        return f"{self.from_table}.{self.from_column}->{self.to_table}.{self.to_column}"


class MeasureValidationCase(Contract):
    """A concrete, checkable expectation for a measure. Produced with the
    measure; executed by QA against DuckDB where the subset allows."""

    id: str = Field(default_factory=lambda: new_id("case"))
    name: str
    filters: dict[str, str] = Field(
        default_factory=dict, description="'Table.Column' -> literal value."
    )
    expected_value: float | None = None
    expected_source: str = Field(
        default="", description="How the expectation was derived independently of the DAX."
    )
    tolerance: float = 0.01
    comparison: str = Field(default="equals", description="equals | not_null | in_range | sign")
    lower: float | None = None
    upper: float | None = None


class MeasureDefinition(Contract):
    id: str = Field(default_factory=lambda: new_id("msr"))
    name: str
    expression: str = Field(description="DAX. Never executed as code; parsed and transpiled.")
    home_table: str = Field(default="", description="Table the measure is attached to.")
    kpi_id: str = Field(default="", description="The KpiRequirement this satisfies.")

    description: str = Field(default="", description="Natural-language explanation.")
    format_string: str = ""
    display_folder: str = ""
    is_hidden: bool = False
    unit: str = ""

    assumptions: list[str] = Field(default_factory=list)
    aggregation_behaviour: str = Field(
        default="",
        description="How it behaves when sliced: additive / semi-additive / non-additive.",
    )
    depends_on_measures: list[str] = Field(default_factory=list)
    depends_on_columns: list[str] = Field(default_factory=list, description="['Table.Column']")
    requires_date_table: bool = False

    validation_cases: list[MeasureValidationCase] = Field(default_factory=list)
    provenance: Provenance | None = None
    confidence: Confidence = Confidence.MEDIUM
    approved: bool | None = None


class ModelIssue(Issue):
    """Alias kept for readability at call sites."""


class SemanticModel(Contract):
    """The Step-5 deliverable, extended in Step 6 with measures."""

    id: str = Field(default_factory=lambda: new_id("model"))
    project_id: str
    name: str = "Semantic Model"
    version: int = 1
    culture: str = "en-US"

    tables: list[TableDefinition] = Field(default_factory=list)
    relationships: list[RelationshipDefinition] = Field(default_factory=list)
    measures: list[MeasureDefinition] = Field(default_factory=list)

    schema_shape: str = Field(default="", description="star | snowflake | flat | mixed")
    design_rationale: str = ""
    issues: list[Issue] = Field(default_factory=list)
    provenance: Provenance | None = None
    generated_at: datetime = Field(default_factory=utcnow)
    approved: bool = False
    approved_at: datetime | None = None

    # ---- lookups -------------------------------------------------------
    def table(self, name: str) -> TableDefinition | None:
        lowered = name.strip().lower()
        return next((t for t in self.tables if t.name.strip().lower() == lowered), None)

    def measure(self, name: str) -> MeasureDefinition | None:
        lowered = name.strip().lower()
        return next((m for m in self.measures if m.name.strip().lower() == lowered), None)

    def resolve_column(self, table: str, column: str) -> ColumnDefinition | None:
        t = self.table(table)
        return t.column(column) if t else None

    @property
    def date_tables(self) -> list[TableDefinition]:
        return [t for t in self.tables if t.role is TableRole.DATE]

    @property
    def fact_tables(self) -> list[TableDefinition]:
        return [t for t in self.tables if t.role is TableRole.FACT]

    @property
    def has_date_dimension(self) -> bool:
        return bool(self.date_tables)

    @property
    def relationships_awaiting_approval(self) -> list[RelationshipDefinition]:
        return [r for r in self.relationships if r.requires_approval and r.approved is None]

    def active_relationships(self) -> list[RelationshipDefinition]:
        return [r for r in self.relationships if r.is_active and r.approved is not False]

    def relationship_path(self, from_table: str, to_table: str) -> list[list[str]]:
        """All simple paths of active relationships between two tables.
        More than one path means an ambiguous filter route."""
        adjacency: dict[str, list[tuple[str, str]]] = {}
        for r in self.active_relationships():
            adjacency.setdefault(r.from_table, []).append((r.to_table, r.key))
            if r.cross_filter is CrossFilter.BOTH:
                adjacency.setdefault(r.to_table, []).append((r.from_table, r.key))
            else:
                # filters flow one -> many as well for traversal purposes when
                # evaluating ambiguity of the *graph*, so record it separately
                adjacency.setdefault(r.to_table, []).append((r.from_table, r.key))
        paths: list[list[str]] = []

        def walk(node: str, seen: set[str], trail: list[str]) -> None:
            if node == to_table and trail:
                paths.append(list(trail))
                return
            for nxt, key in adjacency.get(node, []):
                if nxt in seen:
                    continue
                walk(nxt, seen | {nxt}, trail + [key])

        walk(from_table, {from_table}, [])
        return paths


class SemanticModelAgentInput(Contract):
    project_id: str
    datasets: list[str] = Field(default_factory=list)
    requirements_id: str = ""
    profile_id: str = ""
    join_proposal_ids: list[str] = Field(default_factory=list)


class SemanticModelAgentOutput(Contract):
    model: SemanticModel
    diagram: "ModelDiagram"
    notes: list[str] = Field(default_factory=list)


class DiagramNode(Contract):
    id: str
    label: str
    role: TableRole
    columns: list[str] = Field(default_factory=list)
    row_count: int = 0
    x: float = 0.0
    y: float = 0.0


class DiagramEdge(Contract):
    id: str
    source: str
    target: str
    label: str = ""
    cardinality: Cardinality = Cardinality.MANY_TO_ONE
    cross_filter: CrossFilter = CrossFilter.SINGLE
    is_active: bool = True
    warning: str = ""


class ModelDiagram(Contract):
    """Layout for the Data Model page. Positions are computed server-side
    (facts centred, dimensions on a ring) so the UI needs no layout engine."""

    nodes: list[DiagramNode] = Field(default_factory=list)
    edges: list[DiagramEdge] = Field(default_factory=list)
    mermaid: str = Field(default="", description="Text fallback / export form.")


class DaxAgentInput(Contract):
    project_id: str
    model_id: str
    requirements_id: str = ""
    kpi_ids: list[str] = Field(default_factory=list)
    regenerate_measure_names: list[str] = Field(default_factory=list)
    qa_feedback: list[str] = Field(default_factory=list)


class DaxAgentOutput(Contract):
    measures: list[MeasureDefinition] = Field(default_factory=list)
    rejected: list[dict[str, str]] = Field(
        default_factory=list,
        description="[{kpi, name, expression, reason}] -- generated but failed validation, "
        "kept for the audit trail instead of being silently dropped.",
    )
    notes: list[str] = Field(default_factory=list)


SemanticModelAgentOutput.model_rebuild()
