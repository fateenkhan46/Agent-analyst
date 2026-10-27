"""Measure-level and report-level QA checks."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ...schemas.common import Severity
from ...schemas.qa import (
    TestCategory, TestStatus, ValidationEngine, ValidationResult,
)
from ...schemas.report import ReportSpec
from ...schemas.requirements import RequirementsSpec
from ...schemas.semantic import MeasureDefinition, SemanticModel
from .checks import _result


def check_measures(
    s: Session, project_id: str, model: SemanticModel | None
) -> list[ValidationResult]:
    """Run the three DAX gates over every measure, plus dependency cycles."""
    if model is None:
        return [_result(
            "Measures validate", TestCategory.DAX_SYNTAX, ValidationEngine.DAX_PARSER,
            TestStatus.SKIPPED, expected="every measure parses and resolves",
            skipped_reason="no model",
        )]
    if not model.measures:
        return [_result(
            "Measures exist", TestCategory.KPI_COVERAGE, ValidationEngine.PYTHON,
            TestStatus.FAIL, expected="at least one measure",
            actual="the model has no measures", severity=Severity.HIGH,
            fix="Nothing can be reported without a measure. Check the DAX agent's "
                "rejected list for why none was produced.",
            agent="dax_agent",
        )]

    from ..dax.references import find_dependency_cycles
    from ..dax.validator import evaluate_measure_result, validate_measure

    results: list[ValidationResult] = []
    for measure in model.measures:
        results.extend(validate_measure(measure, model))
        results.append(evaluate_measure_result(s, project_id, measure, model))

    cycles = find_dependency_cycles(model)
    results.append(_result(
        "Measure dependencies are acyclic", TestCategory.DAX_REFERENCES,
        ValidationEngine.DAX_REFERENCE_CHECK,
        TestStatus.PASS if not cycles else TestStatus.FAIL,
        expected="no measure depends on itself, directly or transitively",
        actual=("acyclic" if not cycles
                else "; ".join(" -> ".join(c) for c in cycles[:3])),
        severity=Severity.CRITICAL,
        evidence={"cycles": [" -> ".join(c) for c in cycles]},
        fix="Power BI refuses to load a model with a circular measure dependency.",
        agent="dax_agent",
    ))
    return results


def check_measure_validation_cases(
    s: Session, project_id: str, model: SemanticModel | None
) -> list[ValidationResult]:
    """Execute each measure's own declared expectations.

    These were written alongside the measure, independently of its formula, so
    checking them is checking the measure against intent rather than against
    itself.
    """
    results: list[ValidationResult] = []
    if model is None or not model.measures:
        return results

    from ..dax.runtime import evaluate

    for measure in model.measures:
        for case in measure.validation_cases:
            if any("<each value>" in v for v in case.filters.values()):
                # Covered by the double-counting check, which does the real work.
                continue
            outcome = evaluate(s, project_id, measure, model, filters=case.filters)
            if not outcome.supported:
                results.append(_result(
                    case.name, TestCategory.FILTER_BEHAVIOUR,
                    ValidationEngine.DAX_DUCKDB_TRANSPILED, TestStatus.NOT_SUPPORTED,
                    target=measure.name, expected=case.expected_source,
                    actual=f"not evaluated -- {outcome.unsupported_construct}",
                    skipped_reason=outcome.reason,
                    fix="Outside the evaluable subset; confirm in Power BI Desktop.",
                    agent="dax_agent",
                ))
                continue

            value = outcome.value
            status = TestStatus.PASS
            detail = "BLANK" if value is None else f"{value:,.4f}"

            if case.comparison == "not_null":
                if value is None:
                    status = TestStatus.WARN
            elif case.comparison == "sign":
                if value is not None and case.lower is not None and value < case.lower:
                    status = TestStatus.WARN
            elif case.comparison == "in_range":
                if value is None:
                    status = TestStatus.WARN
                elif (case.lower is not None and value < case.lower) or \
                        (case.upper is not None and value > case.upper):
                    status = TestStatus.FAIL
            elif case.comparison == "equals":
                if case.expected_value is None:
                    # No independent expectation was computed, so there is
                    # nothing to compare against. Reported, not passed.
                    results.append(_result(
                        case.name, TestCategory.FILTER_BEHAVIOUR,
                        ValidationEngine.DAX_DUCKDB_TRANSPILED, TestStatus.SKIPPED,
                        target=measure.name, expected=case.expected_source,
                        actual=detail,
                        skipped_reason="no independently-computed expected value was "
                                       "supplied for this case, so equality could not "
                                       "be checked. The totals-reconciliation check "
                                       "covers the grand total.",
                        agent="dax_agent",
                    ))
                    continue
                if value is None or abs(value - case.expected_value) > case.tolerance:
                    status = TestStatus.FAIL

            results.append(_result(
                case.name, TestCategory.FILTER_BEHAVIOUR,
                ValidationEngine.DAX_DUCKDB_TRANSPILED, status,
                target=measure.name,
                expected=case.expected_source or case.comparison,
                actual=detail,
                severity=Severity.HIGH if status is TestStatus.FAIL else Severity.MEDIUM,
                evidence={"value": value, "filters": case.filters,
                          "comparison": case.comparison,
                          "bounds": [case.lower, case.upper]},
                fix=("" if status is TestStatus.PASS else
                     "The measure returned a value its own stated expectation rules out. "
                     "Either the expectation or the formula is wrong."),
                agent="dax_agent",
            ))
    return results


def check_report(
    report: ReportSpec | None, model: SemanticModel | None,
    requirements: RequirementsSpec | None,
) -> list[ValidationResult]:
    """Visual bindings, KPI coverage and report completeness."""
    results: list[ValidationResult] = []
    if report is None or model is None:
        return [_result(
            "Report visuals bind to real fields", TestCategory.VISUAL_MAPPING,
            ValidationEngine.SCHEMA_VALIDATOR, TestStatus.SKIPPED,
            expected="every binding resolves",
            skipped_reason="the report or the model is missing",
        )]

    broken: list[str] = []
    empty: list[str] = []
    for page in report.pages:
        for visual in page.visuals:
            if visual.visual_type.value == "textbox":
                continue
            if not visual.bindings:
                empty.append(f"{page.name}.{visual.name}")
                continue
            for binding in visual.bindings:
                if binding.is_measure:
                    if model.measure(binding.field) is None:
                        broken.append(
                            f"{page.name}.{visual.name} -> measure [{binding.field}]"
                        )
                elif model.resolve_column(binding.table, binding.field) is None:
                    broken.append(
                        f"{page.name}.{visual.name} -> "
                        f"{binding.table}[{binding.field}]"
                    )
    results.append(_result(
        "Every visual binds to a field that exists", TestCategory.VISUAL_MAPPING,
        ValidationEngine.SCHEMA_VALIDATOR,
        TestStatus.PASS if not broken else TestStatus.FAIL,
        expected=f"all {report.visual_count} visual(s) bind to real model fields",
        actual=("all resolve" if not broken
                else f"{len(broken)} broken binding(s)"),
        severity=Severity.CRITICAL,
        evidence={"broken": broken[:10]},
        fix="A broken binding renders an empty visual in a report that otherwise looks "
            "finished.",
        agent="report_design_agent",
    ))
    if empty:
        results.append(_result(
            "No visual is left without fields", TestCategory.REPORT_COMPLETENESS,
            ValidationEngine.SCHEMA_VALIDATOR, TestStatus.FAIL,
            expected="every visual has at least one field",
            actual=f"{len(empty)} visual(s) have none",
            severity=Severity.HIGH, evidence={"visuals": empty},
            fix="Remove the visual or bind a measure to it.",
            agent="report_design_agent",
        ))

    if requirements and requirements.kpis:
        bound = {
            b.field for p in report.pages for v in p.visuals for b in v.bindings
            if b.is_measure
        }
        missing = [
            k.name for k in requirements.kpis
            if not any(m.name == k.name and m.name in bound for m in model.measures)
        ]
        results.append(_result(
            "Every requested KPI is reported", TestCategory.KPI_COVERAGE,
            ValidationEngine.PYTHON,
            TestStatus.PASS if not missing else TestStatus.WARN,
            expected=f"all {len(requirements.kpis)} requested KPI(s) appear on a visual",
            actual=("all reported" if not missing
                    else f"{len(missing)} not reported: {', '.join(missing)}"),
            severity=Severity.MEDIUM,
            evidence={"missing": missing, "requested": len(requirements.kpis),
                      "reported": len(bound)},
            fix="A requested KPI with no visual is work the stakeholder asked for and "
                "did not get. Either add a visual or record why it was dropped.",
            agent="report_design_agent",
        ))

    results.append(_result(
        "The report has pages", TestCategory.REPORT_COMPLETENESS,
        ValidationEngine.SCHEMA_VALIDATOR,
        TestStatus.PASS if report.pages else TestStatus.FAIL,
        expected="at least one page",
        actual=f"{len(report.pages)} page(s), {report.visual_count} visual(s)",
        severity=Severity.CRITICAL, agent="report_design_agent",
    ))
    return results


def check_assumptions(
    requirements: RequirementsSpec | None, model: SemanticModel | None
) -> list[ValidationResult]:
    """Is anything the numbers depend on still unconfirmed?"""
    results: list[ValidationResult] = []
    if requirements is None:
        return results

    unanswered = [q for q in requirements.open_questions if not q.answered]
    blocking = [q for q in unanswered if q.severity in {Severity.CRITICAL, Severity.HIGH}]
    results.append(_result(
        "No blocking question is unanswered", TestCategory.ASSUMPTIONS,
        ValidationEngine.PYTHON,
        TestStatus.PASS if not blocking else TestStatus.WARN,
        expected="every high-severity question is answered",
        actual=("all answered" if not blocking
                else f"{len(blocking)} unanswered: "
                     + "; ".join(q.question[:70] for q in blocking[:3])),
        severity=Severity.HIGH,
        evidence={"unanswered": [q.question for q in unanswered[:8]]},
        fix="These were raised because the data or the meeting did not settle them. "
            "Answering them may change a definition the numbers already use.",
        agent="requirements_meeting_agent",
    ))

    unconfirmed = [a for a in requirements.assumptions if a.confirmed is None]
    high = [a for a in unconfirmed if a.severity in {Severity.CRITICAL, Severity.HIGH}]
    if unconfirmed:
        results.append(_result(
            "Assumptions are confirmed", TestCategory.ASSUMPTIONS,
            ValidationEngine.PYTHON,
            TestStatus.WARN if high else TestStatus.PASS,
            expected="every high-severity assumption is confirmed",
            actual=f"{len(unconfirmed)} unconfirmed"
                   + (f", {len(high)} of them high severity" if high else ""),
            severity=Severity.HIGH if high else Severity.LOW,
            evidence={"unconfirmed": [a.statement[:120] for a in unconfirmed[:8]]},
            fix="An unconfirmed sign convention or currency treatment silently inverts "
                "or distorts a headline number.",
            agent="requirements_meeting_agent",
        ))

    rejected = [k for k in requirements.kpis if not k.definition]
    if rejected:
        results.append(_result(
            "Every KPI has an agreed definition", TestCategory.KPI_COVERAGE,
            ValidationEngine.PYTHON, TestStatus.WARN,
            expected="every requested KPI has a definition",
            actual=f"{len(rejected)} have none: {', '.join(k.name for k in rejected)}",
            severity=Severity.HIGH,
            evidence={"undefined": [k.name for k in rejected]},
            fix="No measure was generated for these, which is correct: a formula from a "
                "guessed definition produces a confidently wrong number. Supply the "
                "definition and re-run the DAX agent.",
            agent="requirements_meeting_agent",
        ))
    return results


def check_type_conformance(
    execution: Any, profile: Any
) -> list[ValidationResult]:
    """Did the transformation actually fix the type problems it set out to?"""
    results: list[ValidationResult] = []
    if execution is None or execution.quality_after is None:
        return results
    after = execution.quality_after
    poor = {
        reference: share for reference, share in after.type_conformance.items()
        if share < 0.95
    }
    results.append(_result(
        "Prepared columns conform to a single type", TestCategory.TYPE_CONFORMANCE,
        ValidationEngine.PANDAS,
        TestStatus.PASS if not poor else TestStatus.WARN,
        expected="every prepared column is at least 95% one type",
        actual=("all conform" if not poor
                else f"{len(poor)} column(s) do not: "
                     + ", ".join(f"{k} ({v:.0%})" for k, v in list(poor.items())[:4])),
        severity=Severity.MEDIUM,
        evidence={"non_conforming": poor},
        fix="A mixed-type column will not load cleanly into Power BI and may silently "
            "become text, which breaks any measure over it.",
        agent="data_preparation_agent",
    ))

    if execution.quality_before is not None:
        before_score = execution.quality_before.score
        after_score = after.score
        results.append(_result(
            "Data quality did not regress", TestCategory.TYPE_CONFORMANCE,
            ValidationEngine.PANDAS,
            TestStatus.PASS if after_score >= before_score - 0.001 else TestStatus.WARN,
            expected=f"the quality score is at least the pre-transformation "
                     f"{before_score:.4f}",
            actual=f"{after_score:.4f}",
            severity=Severity.MEDIUM,
            evidence={"before": before_score, "after": after_score,
                      "delta": round(after_score - before_score, 4)},
            fix="A transformation that lowers the composite score has traded one "
                "problem for another; check which metric got worse.",
            agent="data_preparation_agent",
        ))
    return results


def check_export(exports: list[Any]) -> list[ValidationResult]:
    """Is the deliverable actually on disk?"""
    from ..storage import get_store

    if not exports:
        return [_result(
            "A downloadable project exists", TestCategory.EXPORT_INTEGRITY,
            ValidationEngine.FILESYSTEM, TestStatus.SKIPPED,
            expected="an exported project",
            skipped_reason="the export step has not run yet",
        )]
    store = get_store()
    results: list[ValidationResult] = []
    for artifact in exports:
        present = store.exists(artifact.stored_path)
        size = store.size(artifact.stored_path) if present else 0
        results.append(_result(
            f"Export '{artifact.filename}' is present and intact",
            TestCategory.EXPORT_INTEGRITY, ValidationEngine.FILESYSTEM,
            TestStatus.PASS if present and size == artifact.size_bytes
            else TestStatus.FAIL,
            target=artifact.filename,
            expected=f"{artifact.size_bytes:,} bytes on disk",
            actual=(f"{size:,} bytes" if present else "the file is missing"),
            severity=Severity.CRITICAL,
            evidence={"stored_path": artifact.stored_path, "sha256": artifact.sha256,
                      "file_count": artifact.file_count},
            fix="Re-run the export; the delivery page must not offer a download that "
                "does not exist.",
            agent="report_design_agent",
        ))
    return results
