"""The deterministic validation suite.

Every check here computes its verdict from the real data or the real artifacts
-- no language model judges anything. ``ValidationResult.engine`` records which
mechanism ran, and a check that could not run returns ``SKIPPED`` or
``NOT_SUPPORTED`` with the reason, never a pass.

The checks are grouped by what they protect:

* **reconciliation** -- do the transformed row counts still account for every
  source row?
* **integrity** -- do keys resolve, are dimensions unique, is cardinality what
  the model claims?
* **double counting** -- the one that actually matters: does a measure summed
  over a dimension's values equal the unsliced total? A fan-out or a dropped
  row shows up here as a number that does not add up.
* **measures** -- syntax, references, execution, and the measure's own
  validation cases.
* **report** -- does every visual bind to something that exists, and is every
  requested KPI reported?
* **assumptions** -- is anything still unconfirmed that the numbers depend on?
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ...schemas.common import Severity
from ...schemas.prep import TransformationExecution, TransformationPlan
from ...schemas.qa import (
    TestCategory, TestStatus, ValidationEngine, ValidationResult,
)
from ...schemas.report import ReportSpec
from ...schemas.requirements import RequirementsSpec
from ...schemas.semantic import SemanticModel, TableRole
from ...schemas.source import SourceDataProfile

#: A sliced total may differ from the unsliced total by at most this share
#: before it is reported. Tiny differences are floating-point noise; anything
#: above this is a dropped row or a fan-out.
SLICE_TOLERANCE = 0.0001


def _result(
    name: str, category: TestCategory, engine: ValidationEngine, status: TestStatus,
    *, target: str = "", expected: str = "", actual: str = "",
    severity: Severity = Severity.INFO, evidence: dict[str, Any] | None = None,
    fix: str = "", agent: str = "", skipped_reason: str = "",
) -> ValidationResult:
    return ValidationResult(
        test_id=f"{category.value}:{target or name}", test_name=name, category=category,
        target=target, expected=expected, actual=actual, status=status, engine=engine,
        severity=severity if status.is_blocking or status is TestStatus.WARN
        else Severity.INFO,
        evidence=evidence or {}, recommended_fix=fix, responsible_agent=agent,
        skipped_reason=skipped_reason,
    )


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------
def check_row_reconciliation(
    profile: SourceDataProfile | None, execution: TransformationExecution | None,
    plan: TransformationPlan | None,
) -> list[ValidationResult]:
    """Does every source row still appear, or is its absence explained?"""
    results: list[ValidationResult] = []
    if profile is None or execution is None:
        results.append(_result(
            "Transformed rows reconcile to the source", TestCategory.ROW_COUNT,
            ValidationEngine.PYTHON, TestStatus.SKIPPED,
            expected="every source row is accounted for",
            skipped_reason="the profile or the transformation execution is missing",
        ))
        return results

    source_rows = {t.table_name: t.row_count for t in profile.tables}
    deltas_by_table: dict[str, list[Any]] = {}
    for delta in execution.row_count_deltas:
        deltas_by_table.setdefault(delta.table, []).append(delta)

    for table, rows in source_rows.items():
        deltas = deltas_by_table.get(table, [])
        if not deltas:
            continue
        final = deltas[-1].rows_after
        removed = sum(-d.delta for d in deltas if d.delta < 0)
        added = sum(d.delta for d in deltas if d.delta > 0)
        explained = [
            f"{d.op.value} removed {-d.delta} row(s)"
            for d in deltas if d.delta < 0
        ]
        balanced = rows + added - removed == final
        results.append(_result(
            f"Row count reconciles for '{table}'", TestCategory.ROW_COUNT,
            ValidationEngine.PYTHON,
            TestStatus.PASS if balanced else TestStatus.FAIL,
            target=table,
            expected=f"{rows} source row(s) +{added} -{removed} = {final}",
            actual=f"{final} row(s) after transformation",
            severity=Severity.CRITICAL,
            evidence={
                "source_rows": rows, "final_rows": final, "added": added,
                "removed": removed, "explained_by": explained,
            },
            fix="Every row that leaves the pipeline must be removed by an approved, "
                "named step. An unexplained drop means a transformation is losing data.",
            agent="data_preparation_agent",
        ))

    for dataset in execution.datasets:
        if dataset.row_count == 0:
            results.append(_result(
                f"Dataset '{dataset.name}' is not empty", TestCategory.ROW_COUNT,
                ValidationEngine.PYTHON, TestStatus.FAIL, target=dataset.name,
                expected="at least one row", actual="0 rows",
                severity=Severity.HIGH,
                fix="An empty dataset makes every measure over it blank. Check the "
                    "transformation steps that produced it.",
                agent="data_preparation_agent",
            ))

    skipped = len(execution.skipped_steps)
    if skipped:
        results.append(_result(
            "All planned transformations were applied", TestCategory.LINEAGE,
            ValidationEngine.PYTHON, TestStatus.WARN,
            expected="every planned step applied",
            actual=f"{skipped} step(s) were skipped (unapproved or rejected)",
            severity=Severity.MEDIUM,
            evidence={"skipped_step_ids": execution.skipped_steps},
            fix="The datasets reflect only the approved steps. Approve the remaining "
                "steps or accept that their effect is absent.",
            agent="data_preparation_agent",
        ))
    return results


def check_lineage(
    execution: TransformationExecution | None, model: SemanticModel | None
) -> list[ValidationResult]:
    """Can every model column be traced back to a source column?"""
    if execution is None or model is None:
        return [_result(
            "Model columns trace back to source columns", TestCategory.LINEAGE,
            ValidationEngine.PYTHON, TestStatus.SKIPPED,
            expected="every column has lineage",
            skipped_reason="the execution or the model is missing",
        )]
    targets = {e.target for e in execution.lineage}
    untraced: list[str] = []
    for table in model.tables:
        if table.role is TableRole.DATE:
            continue  # generated, not derived
        for column in table.columns:
            reference = f"{table.dataset_name}.{column.name}"
            if reference not in targets:
                untraced.append(f"{table.name}[{column.name}]")
    return [_result(
        "Model columns trace back to source columns", TestCategory.LINEAGE,
        ValidationEngine.PYTHON,
        TestStatus.PASS if not untraced else TestStatus.WARN,
        expected="every non-generated column has a lineage edge",
        actual=("all traced" if not untraced
                else f"{len(untraced)} column(s) have no lineage edge"),
        severity=Severity.LOW,
        evidence={"untraced": untraced[:20], "lineage_edges": len(execution.lineage)},
        fix="Columns without lineage cannot be explained to a reviewer who asks where "
            "a number came from.",
        agent="data_preparation_agent",
    )]


# --------------------------------------------------------------------------
# Integrity, computed over the real data
# --------------------------------------------------------------------------
def check_integrity(
    s: Session, project_id: str, model: SemanticModel | None
) -> list[ValidationResult]:
    """Referential integrity, dimension uniqueness and declared cardinality,
    each measured in DuckDB against the data the model will actually load."""
    results: list[ValidationResult] = []
    if model is None:
        return [_result(
            "Referential integrity holds", TestCategory.REFERENTIAL_INTEGRITY,
            ValidationEngine.DUCKDB, TestStatus.SKIPPED,
            expected="every key resolves", skipped_reason="no model",
        )]

    from ..dax.runtime import DuckDbUnavailable, project_connection

    try:
        with project_connection(s, project_id, model) as (con, names, counts):
            if not names:
                return [_result(
                    "Referential integrity holds", TestCategory.REFERENTIAL_INTEGRITY,
                    ValidationEngine.DUCKDB, TestStatus.SKIPPED,
                    expected="every key resolves",
                    skipped_reason="no prepared dataset is loaded for any model table",
                )]

            def q(sql: str) -> Any:
                return con.execute(sql).fetchone()

            for relationship in model.relationships:
                if relationship.approved is False:
                    continue
                child, parent = relationship.from_table, relationship.to_table
                if child not in names or parent not in names:
                    results.append(_result(
                        f"Referential integrity: {relationship.key}",
                        TestCategory.REFERENTIAL_INTEGRITY, ValidationEngine.DUCKDB,
                        TestStatus.SKIPPED, target=relationship.key,
                        expected="every child key resolves in the parent",
                        skipped_reason=f"no data loaded for "
                                       f"{child if child not in names else parent}",
                    ))
                    continue

                orphans = q(
                    f'SELECT COUNT(*) FROM (SELECT DISTINCT "{relationship.from_column}" AS k '
                    f'FROM "{names[child]}" WHERE "{relationship.from_column}" IS NOT NULL) c '
                    f'WHERE c.k NOT IN (SELECT "{relationship.to_column}" '
                    f'FROM "{names[parent]}" WHERE "{relationship.to_column}" IS NOT NULL)'
                )[0]
                total = q(
                    f'SELECT COUNT(DISTINCT "{relationship.from_column}") '
                    f'FROM "{names[child]}"'
                )[0]
                examples = [
                    r[0] for r in con.execute(
                        f'SELECT DISTINCT "{relationship.from_column}" FROM "{names[child]}" c '
                        f'WHERE c."{relationship.from_column}" IS NOT NULL '
                        f'AND c."{relationship.from_column}" NOT IN '
                        f'(SELECT "{relationship.to_column}" FROM "{names[parent]}") LIMIT 5'
                    ).fetchall()
                ]
                results.append(_result(
                    f"Referential integrity: {relationship.key}",
                    TestCategory.REFERENTIAL_INTEGRITY, ValidationEngine.DUCKDB,
                    TestStatus.PASS if not orphans else TestStatus.WARN,
                    target=relationship.key,
                    expected=f"all {total} distinct key(s) resolve in {parent}",
                    actual=("all resolve" if not orphans
                            else f"{orphans} of {total} do not resolve"),
                    severity=Severity.HIGH,
                    evidence={"orphan_keys": orphans, "distinct_keys": total,
                              "examples": examples},
                    fix="Rows with an unmatched key fall outside the dimension. They are "
                        "attributed to the 'UNASSIGNED' member so totals still "
                        "reconcile; confirm that is intended, and never enable 'assume "
                        "referential integrity' on this relationship.",
                    agent="data_preparation_agent",
                ))

                # cardinality: is the declared "one" side actually unique?
                duplicates = q(
                    f'SELECT COUNT(*) FROM (SELECT "{relationship.to_column}" '
                    f'FROM "{names[parent]}" WHERE "{relationship.to_column}" IS NOT NULL '
                    f'GROUP BY 1 HAVING COUNT(*) > 1)'
                )[0]
                results.append(_result(
                    f"Cardinality: {parent}.{relationship.to_column} is unique",
                    TestCategory.CARDINALITY, ValidationEngine.DUCKDB,
                    TestStatus.PASS if not duplicates else TestStatus.FAIL,
                    target=relationship.key,
                    expected=f"{relationship.to_column} is unique in {parent} "
                             f"(a {relationship.cardinality.value} relationship requires it)",
                    actual=("unique" if not duplicates
                            else f"{duplicates} duplicated key value(s)"),
                    severity=Severity.CRITICAL,
                    evidence={"duplicate_key_values": duplicates},
                    fix="A non-unique 'one' side makes the relationship many-to-many: "
                        "Power BI will either refuse it or fan out rows, inflating every "
                        "additive measure. De-duplicate the dimension to one row per key.",
                    agent="semantic_model_agent",
                ))

            for table in model.tables:
                if table.role not in {TableRole.DIMENSION, TableRole.DATE}:
                    continue
                if not table.primary_key or table.name not in names:
                    continue
                key = table.primary_key[0]
                duplicates = q(
                    f'SELECT COUNT(*) FROM (SELECT "{key}" FROM "{names[table.name]}" '
                    f'GROUP BY 1 HAVING COUNT(*) > 1)'
                )[0]
                results.append(_result(
                    f"Dimension key is unique: {table.name}.{key}",
                    TestCategory.DUPLICATES, ValidationEngine.DUCKDB,
                    TestStatus.PASS if not duplicates else TestStatus.FAIL,
                    target=f"{table.name}.{key}",
                    expected="one row per key",
                    actual=("unique" if not duplicates
                            else f"{duplicates} duplicated value(s)"),
                    severity=Severity.CRITICAL,
                    evidence={"duplicates": duplicates,
                              "row_count": counts.get(table.name, 0)},
                    fix="A duplicated dimension key breaks every relationship into it.",
                    agent="data_preparation_agent",
                ))
    except DuckDbUnavailable as exc:
        results.append(_result(
            "Referential integrity holds", TestCategory.REFERENTIAL_INTEGRITY,
            ValidationEngine.DUCKDB, TestStatus.SKIPPED,
            expected="every key resolves", skipped_reason=str(exc),
        ))
    return results


# --------------------------------------------------------------------------
# Double counting -- the check that matters most
# --------------------------------------------------------------------------
def check_double_counting(
    s: Session, project_id: str, model: SemanticModel | None
) -> list[ValidationResult]:
    """Does an additive measure summed over a dimension equal the total?

    This is the single most valuable check in the suite. A relationship that
    fans out makes the slices add to *more* than the total; an unmatched key
    that drops rows makes them add to *less*. Both are invisible in any single
    number, and both are exactly what a reader would never notice.
    """
    results: list[ValidationResult] = []
    if model is None or not model.measures:
        return [_result(
            "Sliced totals equal the unsliced total", TestCategory.DOUBLE_COUNTING,
            ValidationEngine.DAX_DUCKDB_TRANSPILED, TestStatus.SKIPPED,
            expected="slices sum to the total",
            skipped_reason="no model or no measures",
        )]

    from .. import kpi_catalog
    from ..dax.evaluator import FilterContext, Predicate, Unsupported
    from ..dax.runtime import DuckDbUnavailable, project_connection

    additive = [
        m for m in model.measures
        if m.aggregation_behaviour.startswith("additive")
    ]
    if not additive:
        return [_result(
            "Sliced totals equal the unsliced total", TestCategory.DOUBLE_COUNTING,
            ValidationEngine.DAX_DUCKDB_TRANSPILED, TestStatus.SKIPPED,
            expected="slices sum to the total",
            skipped_reason="no measure is declared additive, so the check does not apply",
        )]

    slicers: list[tuple[str, str]] = []
    for table in model.tables:
        if table.role is not TableRole.DIMENSION:
            continue
        for column in table.columns:
            if column.is_hidden or column.is_key:
                continue
            role = kpi_catalog.role_for_column(column.name)
            if role in {"status", "portfolio", "category", "manager", "department"}:
                slicers.append((table.name, column.name))
    if not slicers:
        return [_result(
            "Sliced totals equal the unsliced total", TestCategory.DOUBLE_COUNTING,
            ValidationEngine.DAX_DUCKDB_TRANSPILED, TestStatus.SKIPPED,
            expected="slices sum to the total",
            skipped_reason="the model has no categorical dimension column to slice by",
        )]

    try:
        with project_connection(s, project_id, model) as (con, names, counts):
            if not names:
                return [_result(
                    "Sliced totals equal the unsliced total",
                    TestCategory.DOUBLE_COUNTING,
                    ValidationEngine.DAX_DUCKDB_TRANSPILED, TestStatus.SKIPPED,
                    expected="slices sum to the total",
                    skipped_reason="no prepared dataset is loaded",
                )]
            from ..dax.evaluator import DaxEvaluator

            for measure in additive[:6]:
                for table_name, column in slicers[:3]:
                    if table_name not in names:
                        continue
                    values = [
                        r[0] for r in con.execute(
                            f'SELECT DISTINCT "{column}" FROM "{names[table_name]}" '
                            f'WHERE "{column}" IS NOT NULL ORDER BY 1'
                        ).fetchall()
                    ]
                    if not values or len(values) > 60:
                        continue
                    evaluator = DaxEvaluator(con, model, table_sql_names=names)
                    try:
                        total = evaluator.evaluate_measure(measure)
                        parts: dict[str, float] = {}
                        for value in values:
                            sliced = evaluator.evaluate_measure(
                                measure,
                                filters=FilterContext(predicates=(
                                    Predicate(table=table_name, column=column,
                                              op="=", values=(value,)),
                                )),
                            )
                            parts[str(value)] = float(sliced or 0.0)
                    except Unsupported as exc:
                        results.append(_result(
                            f"'{measure.name}' slices sum to its total over "
                            f"{table_name}[{column}]",
                            TestCategory.DOUBLE_COUNTING,
                            ValidationEngine.DAX_DUCKDB_TRANSPILED,
                            TestStatus.NOT_SUPPORTED,
                            target=f"{measure.name} / {table_name}[{column}]",
                            expected="the sum over every slice equals the unsliced total",
                            actual=f"not checked -- {exc.construct}",
                            skipped_reason=str(exc),
                            fix="This measure is outside the evaluable subset, so "
                                "double counting could NOT be ruled out for it. Verify "
                                "it in Power BI Desktop.",
                            agent="dax_agent",
                        ))
                        continue
                    except Exception as exc:  # noqa: BLE001
                        results.append(_result(
                            f"'{measure.name}' slices sum to its total over "
                            f"{table_name}[{column}]",
                            TestCategory.DOUBLE_COUNTING,
                            ValidationEngine.DAX_DUCKDB_TRANSPILED, TestStatus.ERROR,
                            target=f"{measure.name} / {table_name}[{column}]",
                            expected="the sum over every slice equals the unsliced total",
                            actual=f"{type(exc).__name__}: {exc}",
                            severity=Severity.HIGH, agent="dax_agent",
                        ))
                        continue

                    if total is None:
                        continue
                    summed = sum(parts.values())
                    total_value = float(total)
                    difference = summed - total_value
                    relative = (
                        abs(difference) / abs(total_value) if total_value else
                        (0.0 if not summed else 1.0)
                    )
                    passed = relative <= SLICE_TOLERANCE
                    direction = (
                        "" if passed else
                        "MORE than the total, which means rows are being counted twice "
                        "(a relationship is fanning out)" if difference > 0 else
                        "LESS than the total, which means rows are being dropped (keys "
                        "that do not match the dimension)"
                    )
                    results.append(_result(
                        f"'{measure.name}' slices sum to its total over "
                        f"{table_name}[{column}]",
                        TestCategory.DOUBLE_COUNTING,
                        ValidationEngine.DAX_DUCKDB_TRANSPILED,
                        TestStatus.PASS if passed else TestStatus.FAIL,
                        target=f"{measure.name} / {table_name}[{column}]",
                        expected=f"the {len(values)} slice(s) sum to {total_value:,.4f}",
                        actual=f"they sum to {summed:,.4f}"
                               + (f" -- {difference:+,.4f} ({relative:.2%}) {direction}"
                                  if not passed else ""),
                        severity=Severity.CRITICAL,
                        evidence={
                            "total": total_value, "sum_of_slices": summed,
                            "difference": difference, "relative": relative,
                            "slices": dict(list(parts.items())[:12]),
                        },
                        fix=("" if passed else
                             "Find the relationship whose 'one' side is not unique, or "
                             "the unmatched keys that need an UNASSIGNED dimension "
                             "member. Do not adjust the measure to hide the difference."),
                        agent="semantic_model_agent",
                    ))
    except DuckDbUnavailable as exc:
        results.append(_result(
            "Sliced totals equal the unsliced total", TestCategory.DOUBLE_COUNTING,
            ValidationEngine.DAX_DUCKDB_TRANSPILED, TestStatus.SKIPPED,
            expected="slices sum to the total", skipped_reason=str(exc),
        ))
    return results


def check_totals_against_source(
    s: Session, project_id: str, model: SemanticModel | None,
) -> list[ValidationResult]:
    """Does each additive measure equal a direct SUM of its source column?

    Computed a different way from the DAX, so agreement is real evidence
    rather than the same calculation twice.
    """
    results: list[ValidationResult] = []
    if model is None:
        return results
    from ..dax.runtime import DuckDbUnavailable, project_connection

    try:
        with project_connection(s, project_id, model) as (con, names, _counts):
            if not names:
                return results
            from ..dax.evaluator import DaxEvaluator, Unsupported

            for measure in model.measures:
                if not measure.aggregation_behaviour.startswith("additive"):
                    continue
                if len(measure.depends_on_columns) != 1:
                    continue
                reference = measure.depends_on_columns[0]
                table_name, _, column = reference.partition("[")
                column = column.rstrip("]")
                table_name = table_name.strip("'")
                if table_name not in names:
                    continue
                guard = f"{column}_is_reporting_currency"
                has_guard = guard in [
                    c.name for t in model.tables if t.name == table_name
                    for c in t.columns
                ] and guard in measure.expression
                where = f" WHERE \"{guard}\" = 'True'" if has_guard else ""
                direct = con.execute(
                    f'SELECT SUM(TRY_CAST("{column}" AS DOUBLE)) '
                    f'FROM "{names[table_name]}"{where}'
                ).fetchone()[0]
                evaluator = DaxEvaluator(con, model, table_sql_names=names)
                try:
                    value = evaluator.evaluate_measure(measure)
                except Unsupported:
                    continue
                if direct is None and value is None:
                    continue
                expected_value = float(direct or 0.0)
                actual_value = float(value or 0.0)
                difference = abs(actual_value - expected_value)
                passed = difference < max(0.01, abs(expected_value) * 1e-9)
                results.append(_result(
                    f"'{measure.name}' equals a direct SUM of {reference}",
                    TestCategory.TOTALS_RECONCILIATION,
                    ValidationEngine.DUCKDB,
                    TestStatus.PASS if passed else TestStatus.FAIL,
                    target=measure.name,
                    expected=f"{expected_value:,.4f} (direct SUM"
                             + (", reporting currency only" if has_guard else "")
                             + ")",
                    actual=f"{actual_value:,.4f} (the measure)",
                    severity=Severity.CRITICAL,
                    evidence={
                        "direct_sum": expected_value, "measure_value": actual_value,
                        "difference": actual_value - expected_value,
                        "filtered_to_reporting_currency": has_guard,
                        "method": "computed independently of the DAX, straight from the "
                                  "prepared dataset",
                    },
                    fix="The measure and a direct sum of its own column must agree at "
                        "the grand total. A difference means the measure has an "
                        "unintended filter or the relationship affects the aggregation.",
                    agent="dax_agent",
                ))
    except DuckDbUnavailable:
        return results
    return results
