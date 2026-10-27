"""Standing up DuckDB over a project's prepared datasets.

The connection is in-memory and read-only with respect to the project: the
CSVs are registered as views, nothing is written back. One connection per
evaluation request keeps concurrent requests isolated.
"""
from __future__ import annotations

import re
import time
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any

from sqlalchemy.orm import Session

from ...schemas.semantic import SemanticModel
from .. import repository as repo
from ..storage import get_store


class DuckDbUnavailable(RuntimeError):
    pass


def _safe_relation(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").lower()
    return cleaned or "t"


@contextmanager
def project_connection(
    s: Session, project_id: str, model: SemanticModel
) -> Iterator[tuple[Any, dict[str, str], dict[str, int]]]:
    """Yield (connection, model_table -> relation name, row counts).

    Each model table is matched to a prepared dataset by its ``dataset_name``,
    falling back to its own name. A table with no dataset is simply absent from
    the mapping, and the evaluator reports that as ``missing dataset`` rather
    than silently returning zero.
    """
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise DuckDbUnavailable(
            "duckdb is not installed, so DAX cannot be evaluated against the data. "
            "Install it with: pip install duckdb"
        ) from exc

    store = get_store()
    datasets = {d["name"]: d for d in repo.list_datasets(s, project_id)}
    con = duckdb.connect(":memory:")
    sql_names: dict[str, str] = {}
    row_counts: dict[str, int] = {}
    try:
        for table in model.tables:
            dataset = datasets.get(table.dataset_name) or datasets.get(table.name)
            if dataset is None:
                continue
            try:
                path = store.local_path(dataset["stored_path"])
            except (ValueError, FileNotFoundError):
                continue
            if not path.exists():
                continue
            relation = _safe_relation(table.name)
            # all_varchar so a CSV round-trip never silently mistypes a column;
            # the evaluator casts explicitly where it needs a number or a date.
            # DuckDB cannot bind a parameter inside DDL, so the path is
            # inlined. It comes from our own object store (never from user
            # input) and single quotes are escaped regardless.
            escaped = str(path).replace("'", "''")
            con.execute(
                f'CREATE OR REPLACE VIEW "{relation}" AS SELECT * FROM '
                f"read_csv_auto('{escaped}', header=true, all_varchar=true)"
            )
            sql_names[table.name] = relation
            row_counts[table.name] = int(
                con.execute(f'SELECT COUNT(*) FROM "{relation}"').fetchone()[0]
            )
        yield con, sql_names, row_counts
    finally:
        con.close()


def evaluate(
    s: Session, project_id: str, measure: Any, model: SemanticModel,
    *, filters: dict[str, str] | None = None,
) -> Any:
    """Evaluate one measure, returning an ``EvaluationOutcome``.

    ``filters`` maps ``"Table[Column]"`` to a literal value, which is how the
    DAX Studio page and the QA validation cases express a slice.
    """
    from ...schemas.semantic import MeasureDefinition
    from .evaluator import (
        DaxEvaluator, EvaluationFailed, EvaluationOutcome, FilterContext, Predicate,
        Unsupported,
    )

    if not isinstance(measure, MeasureDefinition):
        measure = MeasureDefinition.model_validate(measure)

    started = time.perf_counter()
    try:
        with project_connection(s, project_id, model) as (con, sql_names, row_counts):
            if not sql_names:
                return EvaluationOutcome(
                    measure=measure.name, supported=False,
                    unsupported_construct="no data",
                    reason="No prepared dataset is loaded for any table in the model, so "
                           "the measure cannot be evaluated. Run the transformation step "
                           "first.",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )

            predicates: list[Predicate] = []
            unknown: list[str] = []
            for reference, value in (filters or {}).items():
                match = re.fullmatch(r"\s*'?([^'\[\]]+)'?\s*\[([^\]]+)\]\s*", reference)
                if not match:
                    unknown.append(reference)
                    continue
                table_name, column_name = match.group(1).strip(), match.group(2).strip()
                table = model.table(table_name)
                if table is None or table.column(column_name) is None:
                    unknown.append(reference)
                    continue
                predicates.append(Predicate(
                    table=table.name, column=table.column(column_name).name,  # type: ignore[union-attr]
                    op="=", values=(value,),
                ))

            evaluator = DaxEvaluator(con, model, table_sql_names=sql_names)
            try:
                value = evaluator.evaluate_measure(
                    measure, filters=FilterContext(predicates=tuple(predicates))
                )
            except Unsupported as exc:
                return EvaluationOutcome(
                    measure=measure.name, supported=False,
                    unsupported_construct=exc.construct, reason=str(exc),
                    sql_executed=evaluator.sql_log, row_counts=row_counts,
                    filters_applied={p.describe(): p.op for p in predicates},
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    notes=evaluator.notes,
                )
            except EvaluationFailed as exc:
                return EvaluationOutcome(
                    measure=measure.name, supported=True, value=None,
                    unsupported_construct="", reason=f"Evaluation failed: {exc}",
                    sql_executed=evaluator.sql_log, row_counts=row_counts,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    notes=evaluator.notes,
                )

            notes = list(evaluator.notes)
            if unknown:
                notes.append(
                    f"{len(unknown)} filter(s) were ignored because they do not resolve "
                    f"against the model: {', '.join(unknown[:5])}"
                )
            return EvaluationOutcome(
                measure=measure.name, supported=True,
                value=None if value is None else float(value),
                is_blank=value is None,
                sql_executed=evaluator.sql_log,
                filters_applied={p.describe(): p.op for p in predicates},
                row_counts=row_counts,
                duration_ms=int((time.perf_counter() - started) * 1000),
                notes=notes,
            )
    except DuckDbUnavailable as exc:
        from .evaluator import EvaluationOutcome as Outcome
        return Outcome(
            measure=measure.name, supported=False,
            unsupported_construct="duckdb unavailable", reason=str(exc),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
