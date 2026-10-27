"""Contracts for Steps 8 and 9: QA and Improvement.

The central honesty mechanism is ``ValidationResult.engine``: it records
*what actually ran*. A measure checked only by the parser carries
``engine="dax_parser"``, not ``"analysis_services"``. Nothing in the codebase
can report a stronger engine than the one that produced the number.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field

from .common import Contract, Provenance, Severity, new_id, utcnow


class TestCategory(str, Enum):
    ROW_COUNT = "row_count"
    DUPLICATES = "duplicates"
    MISSING_KEYS = "missing_keys"
    REFERENTIAL_INTEGRITY = "referential_integrity"
    CARDINALITY = "cardinality"
    TYPE_CONFORMANCE = "type_conformance"
    DAX_SYNTAX = "dax_syntax"
    DAX_REFERENCES = "dax_references"
    DAX_EVALUATION = "dax_evaluation"
    TOTALS_RECONCILIATION = "totals_reconciliation"
    FILTER_BEHAVIOUR = "filter_behaviour"
    DOUBLE_COUNTING = "double_counting"
    VISUAL_MAPPING = "visual_mapping"
    KPI_COVERAGE = "kpi_coverage"
    ASSUMPTIONS = "assumptions"
    REPORT_COMPLETENESS = "report_completeness"
    EXPORT_INTEGRITY = "export_integrity"
    LINEAGE = "lineage"


class ValidationEngine(str, Enum):
    """Which mechanism produced the verdict. Never upgraded optimistically."""

    PYTHON = "python"                   # deterministic code over real data
    PANDAS = "pandas"
    DUCKDB = "duckdb"                   # SQL executed over the real data
    DAX_PARSER = "dax_parser"           # our lexer/parser
    DAX_REFERENCE_CHECK = "dax_reference_check"
    DAX_DUCKDB_TRANSPILED = "dax_duckdb_transpiled"  # DAX subset -> SQL -> executed
    SCHEMA_VALIDATOR = "schema_validator"
    FILESYSTEM = "filesystem"
    LLM_REVIEW = "llm_review"           # advisory only; never a pass/fail gate alone
    ANALYSIS_SERVICES = "analysis_services"  # never set in this environment

    @property
    def deterministic(self) -> bool:
        return self is not ValidationEngine.LLM_REVIEW


class TestStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIPPED = "skipped"
    NOT_SUPPORTED = "not_supported"
    ERROR = "error"

    @property
    def is_blocking(self) -> bool:
        return self in {TestStatus.FAIL, TestStatus.ERROR}


class ValidationTest(Contract):
    """The *declaration* of a check: what will be asserted and how."""

    id: str = Field(default_factory=lambda: new_id("test"))
    name: str
    category: TestCategory
    description: str = ""
    target: str = Field(default="", description="table / table.column / measure / page.visual")
    expected: str = Field(description="Human-readable expectation.")
    engine: ValidationEngine
    severity_if_failed: Severity = Severity.HIGH
    responsible_agent: str = Field(
        default="", description="Agent the QA agent returns the work to on failure."
    )


class ValidationResult(Contract):
    """The *outcome*. Always carries evidence a human can re-check."""

    id: str = Field(default_factory=lambda: new_id("res"))
    test_id: str
    test_name: str
    category: TestCategory
    target: str = ""
    expected: str = ""
    actual: str = ""
    status: TestStatus
    engine: ValidationEngine
    severity: Severity = Severity.INFO
    evidence: dict[str, Any] = Field(default_factory=dict)
    recommended_fix: str = ""
    responsible_agent: str = ""
    duration_ms: int = 0
    skipped_reason: str = ""
    ran_at: datetime = Field(default_factory=utcnow)


class ReworkRequest(Contract):
    """QA returning work to an agent. The orchestrator turns this into a new
    ``AgentTask`` with ``requested_by='qa_agent'``."""

    id: str = Field(default_factory=lambda: new_id("rework"))
    target_agent: str
    reason: str
    failed_result_ids: list[str] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    severity: Severity = Severity.HIGH


class ValidationReport(Contract):
    """The Step-8 deliverable."""

    id: str = Field(default_factory=lambda: new_id("qa"))
    project_id: str
    model_id: str = ""
    report_id: str = ""
    generated_at: datetime = Field(default_factory=utcnow)
    results: list[ValidationResult] = Field(default_factory=list)
    rework_requests: list[ReworkRequest] = Field(default_factory=list)
    provenance: Provenance | None = None

    validation_scope_note: str = Field(
        default="",
        description="Explicit statement of what could NOT be validated in this "
        "environment -- printed alongside every summary.",
    )

    @property
    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in TestStatus}
        for r in self.results:
            out[r.status.value] += 1
        return out

    @property
    def passed(self) -> bool:
        """True only when nothing blocking failed. Tests that could not run
        do not count as passes."""
        return not any(r.status.is_blocking for r in self.results)

    @property
    def blocking_failures(self) -> list[ValidationResult]:
        return [r for r in self.results if r.status.is_blocking]

    def engines_used(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            if r.status not in {TestStatus.SKIPPED, TestStatus.NOT_SUPPORTED}:
                out[r.engine.value] = out.get(r.engine.value, 0) + 1
        return out


# --------------------------------------------------------------------------
# Improvement agent
# --------------------------------------------------------------------------
class ImprovementArea(str, Enum):
    TRANSFORMATION = "transformation"
    JOIN_STRATEGY = "join_strategy"
    MODELLING = "modelling"
    MISSING_KPI = "missing_kpi"
    MEASURE_QUALITY = "measure_quality"
    PERFORMANCE = "performance"
    METRIC_CONSISTENCY = "metric_consistency"
    ADDITIONAL_INSIGHT = "additional_insight"
    VISUALISATION = "visualisation"
    BUSINESS_PROCESS = "business_process"
    DATA_COLLECTION = "data_collection"


class ImprovementRecommendation(Contract):
    id: str = Field(default_factory=lambda: new_id("imp"))
    project_id: str
    area: ImprovementArea
    title: str
    rationale: str = Field(description="Why this is worth doing.")
    evidence: list[str] = Field(
        default_factory=list, description="Measured facts supporting it."
    )
    expected_benefit: str = ""
    quantified_benefit: str = Field(
        default="", description="Only populated when it can actually be quantified."
    )
    trade_offs: list[str] = Field(default_factory=list)
    effort: str = Field(default="medium", description="low | medium | high")
    severity: Severity = Severity.MEDIUM
    affected_objects: list[str] = Field(default_factory=list)
    proposed_change: dict[str, Any] = Field(
        default_factory=dict,
        description="Machine-applicable payload. Applied ONLY after approval.",
    )
    auto_applicable: bool = False
    state: str = Field(default="proposed", description="proposed | approved | rejected | applied")
    decided_by: str = ""
    provenance: Provenance | None = None


class ImprovementReport(Contract):
    id: str = Field(default_factory=lambda: new_id("improve"))
    project_id: str
    generated_at: datetime = Field(default_factory=utcnow)
    recommendations: list[ImprovementRecommendation] = Field(default_factory=list)
    summary: str = ""

    def by_area(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.recommendations:
            out[r.area.value] = out.get(r.area.value, 0) + 1
        return out


class QaAgentInput(Contract):
    project_id: str
    model_id: str = ""
    report_id: str = ""
    plan_id: str = ""
    execution_id: str = ""
    requirements_id: str = ""
    categories: list[TestCategory] = Field(default_factory=list)


class QaAgentOutput(Contract):
    report: ValidationReport
    notes: list[str] = Field(default_factory=list)


class ImprovementAgentInput(Contract):
    project_id: str
    model_id: str = ""
    report_id: str = ""
    plan_id: str = ""
    requirements_id: str = ""
    validation_report_id: str = ""


class ImprovementAgentOutput(Contract):
    report: ImprovementReport
    notes: list[str] = Field(default_factory=list)
