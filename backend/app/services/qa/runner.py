"""Running the QA suite and routing failures back to the responsible agent.

Two things make this an *independent* reviewer rather than a rubber stamp:

1. It recomputes from the real data and the real artifacts, rather than
   trusting any agent's self-report.
2. Every failure carries ``responsible_agent``, and those become
   ``ReworkRequest`` records the orchestrator turns into real tasks. QA can
   therefore send work back, which is what the brief asks for.

``validation_scope_note`` is printed alongside every summary and states what
could *not* be checked in this environment. A green report that quietly
omitted half the checks would be worse than a red one.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ...schemas.common import Confidence, Origin, Provenance, Severity
from ...schemas.prep import TransformationExecution, TransformationPlan
from ...schemas.qa import (
    QaAgentInput, ReworkRequest, TestCategory, TestStatus, ValidationEngine,
    ValidationReport,
)
from ...schemas.report import ReportSpec
from ...schemas.requirements import RequirementsSpec
from ...schemas.semantic import SemanticModel
from ...schemas.source import SourceDataProfile
from .. import repository as repo
from .checks import (
    check_double_counting, check_integrity, check_lineage, check_row_reconciliation,
    check_totals_against_source,
)
from .measures import (
    check_assumptions, check_export, check_measure_validation_cases, check_measures,
    check_report, check_type_conformance,
)

#: Which categories a failure should be routed to which agent for. A result
#: already carries ``responsible_agent``; this is the fallback.
CATEGORY_OWNER = {
    TestCategory.ROW_COUNT: "data_preparation_agent",
    TestCategory.DUPLICATES: "data_preparation_agent",
    TestCategory.MISSING_KEYS: "data_preparation_agent",
    TestCategory.REFERENTIAL_INTEGRITY: "data_preparation_agent",
    TestCategory.TYPE_CONFORMANCE: "data_preparation_agent",
    TestCategory.LINEAGE: "data_preparation_agent",
    TestCategory.CARDINALITY: "semantic_model_agent",
    TestCategory.DOUBLE_COUNTING: "semantic_model_agent",
    TestCategory.DAX_SYNTAX: "dax_agent",
    TestCategory.DAX_REFERENCES: "dax_agent",
    TestCategory.DAX_EVALUATION: "dax_agent",
    TestCategory.TOTALS_RECONCILIATION: "dax_agent",
    TestCategory.FILTER_BEHAVIOUR: "dax_agent",
    TestCategory.VISUAL_MAPPING: "report_design_agent",
    TestCategory.REPORT_COMPLETENESS: "report_design_agent",
    TestCategory.KPI_COVERAGE: "report_design_agent",
    TestCategory.ASSUMPTIONS: "requirements_meeting_agent",
    TestCategory.EXPORT_INTEGRITY: "report_design_agent",
}


def run_qa(
    s: Session, payload: QaAgentInput, *, actor: str = "qa_agent"
) -> ValidationReport:
    project_id = payload.project_id
    profile = repo.load_artifact(s, project_id, repo.ArtifactKind.PROFILE, SourceDataProfile)
    requirements = repo.load_artifact(
        s, project_id, repo.ArtifactKind.REQUIREMENTS, RequirementsSpec
    )
    plan = repo.load_artifact(s, project_id, repo.ArtifactKind.PLAN, TransformationPlan)
    execution = repo.load_artifact(
        s, project_id, repo.ArtifactKind.EXECUTION, TransformationExecution
    )
    model = repo.load_artifact(s, project_id, repo.ArtifactKind.MODEL, SemanticModel)
    report_spec = repo.load_artifact(s, project_id, repo.ArtifactKind.REPORT, ReportSpec)
    exports = repo.list_exports(s, project_id)

    wanted = set(payload.categories) if payload.categories else None

    def include(*categories: TestCategory) -> bool:
        return wanted is None or bool(wanted & set(categories))

    results = []
    if include(TestCategory.ROW_COUNT, TestCategory.LINEAGE):
        results += check_row_reconciliation(profile, execution, plan)
        results += check_lineage(execution, model)
    if include(TestCategory.TYPE_CONFORMANCE):
        results += check_type_conformance(execution, profile)
    if include(TestCategory.REFERENTIAL_INTEGRITY, TestCategory.CARDINALITY,
               TestCategory.DUPLICATES):
        results += check_integrity(s, project_id, model)
    if include(TestCategory.DAX_SYNTAX, TestCategory.DAX_REFERENCES,
               TestCategory.DAX_EVALUATION):
        results += check_measures(s, project_id, model)
    if include(TestCategory.TOTALS_RECONCILIATION):
        results += check_totals_against_source(s, project_id, model)
    if include(TestCategory.DOUBLE_COUNTING):
        results += check_double_counting(s, project_id, model)
    if include(TestCategory.FILTER_BEHAVIOUR):
        results += check_measure_validation_cases(s, project_id, model)
    if include(TestCategory.VISUAL_MAPPING, TestCategory.REPORT_COMPLETENESS,
               TestCategory.KPI_COVERAGE):
        results += check_report(report_spec, model, requirements)
    if include(TestCategory.ASSUMPTIONS):
        results += check_assumptions(requirements, model)
    if include(TestCategory.EXPORT_INTEGRITY):
        results += check_export(exports)

    report = ValidationReport(
        project_id=project_id,
        model_id=model.id if model else "",
        report_id=report_spec.id if report_spec else "",
        results=results,
        validation_scope_note=_scope_note(results, model),
        provenance=Provenance(
            origin=Origin.DETERMINISTIC, produced_by=actor, confidence=Confidence.HIGH,
            rationale="Every verdict is recomputed from the stored artifacts and the "
                      "prepared data. No language model judges any result, and a check "
                      "that could not run is recorded as skipped or unsupported rather "
                      "than as a pass.",
            evidence=[
                f"{len(results)} check(s) across "
                f"{len({r.category for r in results})} category(ies)",
                "engines used: " + ", ".join(
                    f"{k} x{v}" for k, v in sorted(
                        _engine_counts(results).items()
                    )
                ),
            ],
        ),
    )
    report.rework_requests = build_rework(report)
    return report


def _engine_counts(results: list) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        if r.status in {TestStatus.SKIPPED, TestStatus.NOT_SUPPORTED}:
            continue
        out[r.engine.value] = out.get(r.engine.value, 0) + 1
    return out


def _scope_note(results: list, model: SemanticModel | None) -> str:
    """State plainly what was NOT verified. Printed with every summary."""
    not_supported = [r for r in results if r.status is TestStatus.NOT_SUPPORTED]
    skipped = [r for r in results if r.status is TestStatus.SKIPPED]
    parts = [
        "This report was produced without a Power BI engine. No measure was executed "
        "by Analysis Services, and no .pbix was rendered.",
        "What WAS verified: row-count reconciliation, referential integrity, "
        "dimension-key uniqueness and relationship cardinality measured in DuckDB "
        "against the prepared data; DAX syntax and model-aware reference resolution; "
        "and, for measures inside the evaluable subset, actual execution over the real "
        "data with totals cross-checked against a direct SUM computed independently of "
        "the DAX.",
    ]
    if not_supported:
        names = sorted({r.target or r.test_name for r in not_supported})
        parts.append(
            f"NOT verified numerically: {len(not_supported)} check(s) involve DAX "
            f"outside the evaluable subset ({', '.join(names[:6])}"
            + (", ..." if len(names) > 6 else "")
            + "). Their syntax and references were checked; their values were not. "
              "Confirm those in Power BI Desktop."
        )
    if skipped:
        reasons = sorted({r.skipped_reason for r in skipped if r.skipped_reason})
        parts.append(
            f"{len(skipped)} check(s) could not run: "
            + "; ".join(reasons[:4])
            + ("; ..." if len(reasons) > 4 else "")
            + "."
        )
    if model and not model.has_date_dimension:
        parts.append(
            "The model has no date dimension, so no time-intelligence behaviour was "
            "checked."
        )
    parts.append(
        "'Mark as date table' cannot be set from TMDL, so time-intelligence measures "
        "are only correct once that is done in Desktop -- this suite cannot verify it."
    )
    return " ".join(parts)


def build_rework(report: ValidationReport) -> list[ReworkRequest]:
    """Group blocking failures by the agent that must fix them."""
    by_agent: dict[str, list] = {}
    for result in report.results:
        if not result.status.is_blocking:
            continue
        agent = result.responsible_agent or CATEGORY_OWNER.get(result.category, "")
        if not agent:
            continue
        by_agent.setdefault(agent, []).append(result)

    requests: list[ReworkRequest] = []
    for agent, failures in by_agent.items():
        worst = max(
            (f.severity for f in failures),
            key=lambda s: ["info", "low", "medium", "high", "critical"].index(s.value),
        )
        requests.append(ReworkRequest(
            target_agent=agent,
            reason=f"{len(failures)} blocking QA failure(s) in "
                   f"{', '.join(sorted({f.category.value for f in failures}))}: "
                   + "; ".join(f"{f.test_name} -- {f.actual}" for f in failures[:3]),
            failed_result_ids=[f.id for f in failures],
            instructions=[
                f.recommended_fix for f in failures if f.recommended_fix
            ][:6],
            severity=worst,
        ))
    return requests
