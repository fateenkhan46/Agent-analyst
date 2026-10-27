"""The three validation gates, as ``ValidationResult`` records.

``ValidationResult.engine`` always names what actually ran, so nothing can be
reported as more thoroughly checked than it was:

* ``dax_parser``              -- the expression parses
* ``dax_reference_check``     -- every name resolves against the model
* ``dax_duckdb_transpiled``   -- the measure was executed against real data

A measure outside the evaluable subset gets ``NOT_SUPPORTED`` with the
construct named. It is never a pass.
"""
from __future__ import annotations

import time

from sqlalchemy.orm import Session

from ...schemas.common import Severity
from ...schemas.qa import (
    TestCategory, TestStatus, ValidationEngine, ValidationResult,
)
from ...schemas.semantic import MeasureDefinition, SemanticModel
from .lexer import DaxSyntaxError
from .parser import parse, to_source
from .references import validate_references


def _result(
    *, name: str, category: TestCategory, engine: ValidationEngine, status: TestStatus,
    target: str, expected: str, actual: str, severity: Severity = Severity.INFO,
    evidence: dict | None = None, fix: str = "", duration_ms: int = 0,
    skipped_reason: str = "",
) -> ValidationResult:
    return ValidationResult(
        test_id=f"{category.value}:{target}", test_name=name, category=category,
        target=target, expected=expected, actual=actual, status=status, engine=engine,
        severity=severity, evidence=evidence or {}, recommended_fix=fix,
        responsible_agent="dax_agent", duration_ms=duration_ms,
        skipped_reason=skipped_reason,
    )


def validate_measure(
    measure: MeasureDefinition, model: SemanticModel
) -> list[ValidationResult]:
    """Gates 1 and 2: syntax and model-aware references. No data needed."""
    results: list[ValidationResult] = []
    target = measure.name

    started = time.perf_counter()
    try:
        tree = parse(measure.expression)
        normalised = to_source(tree)
        results.append(_result(
            name="DAX parses", category=TestCategory.DAX_SYNTAX,
            engine=ValidationEngine.DAX_PARSER, status=TestStatus.PASS, target=target,
            expected="the expression is syntactically valid DAX",
            actual="parsed successfully",
            evidence={"normalised": normalised[:600]},
            duration_ms=int((time.perf_counter() - started) * 1000),
        ))
    except DaxSyntaxError as exc:
        results.append(_result(
            name="DAX parses", category=TestCategory.DAX_SYNTAX,
            engine=ValidationEngine.DAX_PARSER, status=TestStatus.FAIL, target=target,
            expected="the expression is syntactically valid DAX",
            actual=str(exc), severity=Severity.CRITICAL,
            evidence={"expression": measure.expression[:600],
                      "position": getattr(exc, "position", 0)},
            fix="Correct the syntax error at the reported position.",
            duration_ms=int((time.perf_counter() - started) * 1000),
        ))
        # No point resolving references in an unparseable expression.
        results.append(_result(
            name="DAX references resolve", category=TestCategory.DAX_REFERENCES,
            engine=ValidationEngine.DAX_REFERENCE_CHECK, status=TestStatus.SKIPPED,
            target=target, expected="every table, column, measure and function exists",
            actual="not checked", skipped_reason="the expression does not parse",
        ))
        return results

    started = time.perf_counter()
    refs = validate_references(
        measure.expression, model, measure_name=measure.name,
        home_table=measure.home_table,
    )
    blocking = refs.blocking
    advisory = [i for i in refs.issues if not i.blocking]
    duration = int((time.perf_counter() - started) * 1000)

    if blocking:
        results.append(_result(
            name="DAX references resolve", category=TestCategory.DAX_REFERENCES,
            engine=ValidationEngine.DAX_REFERENCE_CHECK, status=TestStatus.FAIL,
            target=target,
            expected="every table, column, measure and function exists in the model",
            actual="; ".join(i.message for i in blocking[:4]),
            severity=max(
                (i.severity for i in blocking),
                key=lambda s: ["info", "low", "medium", "high", "critical"].index(s.value),
            ),
            evidence={
                "issues": [
                    {"code": i.code, "message": i.message, "position": i.position,
                     "target": i.target, "suggestion": i.suggestion}
                    for i in blocking
                ],
                "columns_referenced": refs.referenced_columns,
                "measures_referenced": refs.referenced_measures,
            },
            fix="; ".join(
                f"{i.target}: {i.suggestion}" for i in blocking if i.suggestion
            ) or "Correct the references named above.",
            duration_ms=duration,
        ))
    else:
        results.append(_result(
            name="DAX references resolve", category=TestCategory.DAX_REFERENCES,
            engine=ValidationEngine.DAX_REFERENCE_CHECK, status=TestStatus.PASS,
            target=target,
            expected="every table, column, measure and function exists in the model",
            actual=f"{len(refs.referenced_columns)} column(s), "
                   f"{len(refs.referenced_measures)} measure(s), "
                   f"{len(set(refs.functions_used))} function(s) all resolved",
            evidence={
                "columns": refs.referenced_columns, "measures": refs.referenced_measures,
                "functions": sorted(set(refs.functions_used)),
            },
            duration_ms=duration,
        ))

    if advisory:
        results.append(_result(
            name="DAX usage review", category=TestCategory.DAX_REFERENCES,
            engine=ValidationEngine.DAX_REFERENCE_CHECK, status=TestStatus.WARN,
            target=target, expected="no questionable constructs",
            actual="; ".join(i.message for i in advisory[:3]),
            severity=Severity.LOW,
            evidence={"notes": [
                {"code": i.code, "message": i.message, "suggestion": i.suggestion}
                for i in advisory
            ]},
            fix="Advisory only; review the notes.",
        ))
    return results


def evaluate_measure_result(
    s: Session, project_id: str, measure: MeasureDefinition, model: SemanticModel,
) -> ValidationResult:
    """Gate 3: execute against the real data where the subset allows."""
    from .runtime import evaluate

    outcome = evaluate(s, project_id, measure, model)
    target = measure.name

    if not outcome.supported:
        return _result(
            name="DAX evaluates against the data",
            category=TestCategory.DAX_EVALUATION,
            engine=ValidationEngine.DAX_DUCKDB_TRANSPILED,
            status=TestStatus.NOT_SUPPORTED, target=target,
            expected="the measure returns a value when executed over the prepared data",
            actual=f"not evaluated -- {outcome.unsupported_construct}",
            skipped_reason=outcome.reason,
            evidence={"construct": outcome.unsupported_construct,
                      "expression": measure.expression[:400]},
            fix="This construct is outside the evaluable subset (docs/DAX_SUBSET.md). "
                "Syntax and references were still checked; the numeric result was NOT "
                "verified and must be confirmed in Power BI Desktop.",
            duration_ms=outcome.duration_ms,
        )

    if outcome.value is None and outcome.reason:
        return _result(
            name="DAX evaluates against the data",
            category=TestCategory.DAX_EVALUATION,
            engine=ValidationEngine.DAX_DUCKDB_TRANSPILED,
            status=TestStatus.FAIL, target=target, severity=Severity.HIGH,
            expected="the measure returns a value when executed over the prepared data",
            actual=outcome.reason,
            evidence={"sql": outcome.sql_executed[-3:], "notes": outcome.notes},
            fix="Fix the expression so it evaluates over the real data.",
            duration_ms=outcome.duration_ms,
        )

    return _result(
        name="DAX evaluates against the data",
        category=TestCategory.DAX_EVALUATION,
        engine=ValidationEngine.DAX_DUCKDB_TRANSPILED,
        status=TestStatus.PASS, target=target,
        expected="the measure returns a value when executed over the prepared data",
        actual=("BLANK" if outcome.is_blank else f"{outcome.value:,.4f}"),
        evidence={
            "value": outcome.value, "is_blank": outcome.is_blank,
            "row_counts": outcome.row_counts, "sql": outcome.sql_executed[-3:],
            "notes": outcome.notes,
            "engine_note": "Executed by DuckDB over the prepared CSVs, NOT by the Power BI "
                           "engine. The arithmetic is verified; VertiPaq-specific behaviour "
                           "is not.",
        },
        duration_ms=outcome.duration_ms,
    )
