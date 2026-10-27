"""Running a transformation plan end to end.

Order of business:

1. Measure quality **before**.
2. Apply approved steps in ordinal order, recording a row-count delta and
   lineage per step. Unapproved or rejected steps are skipped and listed.
3. Apply approved joins.
4. Measure quality **after**, and compare.
5. Write each resulting dataset to storage as CSV, and emit the Power Query M
   that reproduces the same work inside Power BI.

The datasets are written as CSV rather than Parquet because the PBIP project
has to be able to read them with ``Csv.Document`` -- Power BI cannot open a
Parquet file from a local folder without extra connectors, and a deliverable
the user cannot open is not a deliverable.
"""
from __future__ import annotations

import io
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from ...schemas.agent import AuditAction
from ...schemas.common import Severity, utcnow
from ...schemas.prep import (
    PreparedDataset, RowCountDelta, TransformOp, TransformationExecution,
    TransformationPlan,
)
from .. import repository as repo
from ..storage import get_store, project_key
from .executor import HANDLERS, ExecContext, TransformFailed
from .mquery import build_dataset_query, build_source_query
from .quality import compare, measure

#: Ops that create a new dataset rather than modifying an existing one.
CREATING_OPS = {
    TransformOp.APPEND_TABLES, TransformOp.BUILD_DATE_TABLE,
}


def _role_hint(name: str, frame: pd.DataFrame) -> str:
    """fact | dimension | date, from the table's measured shape.

    The deciding test is **key uniqueness**, not the presence of a measure: a
    project register carries a budget and a completion percentage but still has
    one row per project, which makes it a dimension. Calling it a fact would
    produce a model that double-counts budget across cost rows.
    """
    from .. import kpi_catalog

    columns = [str(c) for c in frame.columns]
    roles = {c: kpi_catalog.role_for_column(c) for c in columns}
    if name.startswith("dim_date") or name == "date" or \
            {"month_key", "date_key"} <= set(columns):
        return "date"
    if name.startswith("dim_"):
        return "dimension"

    has_measure = any(r in kpi_catalog.MEASURE_ROLES for r in roles.values())
    key_columns = [c for c, r in roles.items() if r in kpi_catalog.KEY_ROLES]
    unique_key = any(
        len(frame) > 0 and frame[c].astype(str).nunique(dropna=False) == len(frame)
        for c in key_columns
    )
    if unique_key:
        # One row per entity: a dimension, whatever numeric columns it carries.
        return "dimension"
    return "fact" if has_measure else "dimension"


def execute_plan(
    s: Session, project_id: str, plan: TransformationPlan,
    *, frames: dict[str, pd.DataFrame], actor: str = "data_preparation_agent",
) -> TransformationExecution:
    execution = TransformationExecution(project_id=project_id, plan_id=plan.id)
    ctx = ExecContext({name: frame.copy() for name, frame in frames.items()})

    key_pairs = [
        (f"{j.left_table}.{j.left_keys[0]}", f"{j.right_table}.{j.right_keys[0]}")
        for j in plan.join_proposals if j.left_keys and j.right_keys
    ]
    execution.quality_before = measure(
        project_id, "before", ctx.frames, key_pairs=key_pairs
    )

    # Record where every column started, for lineage.
    for table, frame in ctx.frames.items():
        for col in frame.columns:
            ctx.record_lineage(table, str(col), [f"{table}.{col}"], "source", "source column")

    # ------------------------------------------------------------------
    # Apply the steps
    # ------------------------------------------------------------------
    for step in plan.ordered_steps():
        if step.requires_approval and step.approved is not True:
            reason = "rejected by the user" if step.approved is False else "awaiting approval"
            execution.skipped_steps.append(step.id)
            execution.log.append(
                f"SKIPPED [{step.ordinal}] {step.title or step.op.value} on "
                f"{step.target_table}: {reason}."
            )
            continue

        handler = HANDLERS.get(step.op)
        if handler is None:
            execution.log.append(
                f"SKIPPED [{step.ordinal}] {step.op.value}: no handler is registered."
            )
            execution.skipped_steps.append(step.id)
            continue

        target = step.target_table
        creating = step.op in CREATING_OPS or (
            step.op is TransformOp.BUILD_DIMENSION and bool(step.params.get("from_columns"))
        )
        frame = ctx.frames.get(target)
        if frame is None and not creating:
            execution.log.append(
                f"SKIPPED [{step.ordinal}] {step.op.value}: table '{target}' does not exist."
            )
            execution.skipped_steps.append(step.id)
            continue
        if frame is None:
            frame = pd.DataFrame()

        rows_before = len(frame)
        try:
            result, notes = handler(frame, step.params, ctx)
        except Exception as exc:  # noqa: BLE001 - recorded and reported
            execution.failed_step_id = step.id
            execution.error = (
                f"Step {step.ordinal} ({step.op.value} on {target}) failed: "
                f"{type(exc).__name__}: {exc}"
            )
            execution.log.append(f"FAILED [{step.ordinal}] {execution.error}")
            execution.finished_at = utcnow()
            repo.record_audit(
                s, project_id=project_id, actor=actor, action=AuditAction.ERROR,
                severity=Severity.HIGH, summary=execution.error, target=step.id,
            )
            return execution

        ctx.frames[target] = result
        execution.row_count_deltas.append(RowCountDelta(
            step_id=step.id, op=step.op, table=target,
            rows_before=rows_before, rows_after=len(result),
        ))
        execution.log.append(
            f"[{step.ordinal}] {step.op.value} on {target}: " + "; ".join(notes)
        )

        # lineage for columns this step created or changed
        if step.op is TransformOp.RENAME_COLUMN:
            for src, dst in step.params.get("renames", {}).items():
                ctx.record_lineage(target, dst, [f"{target}.{src}"], step.id,
                                   f"renamed from '{src}'")
        elif step.op is TransformOp.APPEND_TABLES:
            for col in result.columns:
                ctx.record_lineage(
                    target, str(col),
                    [f"{t}.{col}" for t in step.params.get("tables", [])],
                    step.id, "appended from several tables",
                )
        else:
            for col in step.affects_columns or []:
                ctx.record_lineage(
                    target, col, [f"{target}.{col}"], step.id,
                    step.title or step.op.value,
                )
            for col in result.columns:
                if col not in frame.columns:
                    base = str(col)
                    for suffix in ("_unparsed", "_currency", "_is_missing", "_original",
                                   "_source_format", "_is_reporting_currency"):
                        if base.endswith(suffix):
                            base = base[: -len(suffix)]
                            break
                    ctx.record_lineage(
                        target, str(col),
                        [f"{target}.{base}"] if base != str(col) else [],
                        step.id, f"added by {step.op.value}",
                    )

    # ------------------------------------------------------------------
    # Apply approved joins
    # ------------------------------------------------------------------
    for join in plan.join_proposals:
        if not join.recommended or join.confidence.value == "low":
            execution.log.append(
                f"JOIN NOT APPLIED {join.left_table} -> {join.right_table}: "
                + ("not recommended" if not join.recommended else "low confidence")
                + ". It is proposed to the semantic model as a relationship instead of "
                  "being materialised, which avoids row fan-out."
            )
            continue
        # A relationship in the model is better than a physical join: it keeps
        # the fact table at its own grain. We therefore record the join as
        # *evidence for a relationship* and do not materialise it.
        execution.log.append(
            f"JOIN {join.left_table}.{join.left_keys[0]} -> "
            f"{join.right_table}.{join.right_keys[0]} ({join.match_share:.1%} matched) "
            f"will become a model relationship rather than a physical join, so "
            f"{join.left_table} keeps its transaction grain."
        )

    # ------------------------------------------------------------------
    # Quality after, and persist
    # ------------------------------------------------------------------
    outputs = {
        name: frame for name, frame in ctx.frames.items()
        if name in plan.output_datasets or name.startswith("dim_")
    } or ctx.frames
    execution.quality_after = measure(project_id, "after", outputs, key_pairs=key_pairs)
    execution.lineage = ctx.lineage

    store = get_store()

    # ------------------------------------------------------------------
    # Ship the RAW extract of every source table too, so the generated
    # Power Query reads real inputs and its cleaning steps actually run.
    # Without this the M would read an already-cleaned file and re-apply
    # the same transformations to it.
    # ------------------------------------------------------------------
    raw_columns: dict[str, list[str]] = {}
    for name, frame in frames.items():
        buffer = io.StringIO()
        frame.to_csv(buffer, index=False)
        store.put_bytes(
            project_key(project_id, "datasets", "raw", f"{name}.csv"),
            buffer.getvalue().encode("utf-8"),
        )
        raw_columns[name] = [str(c) for c in frame.columns]

    # The M is emitted in two layers: one query per source table (read raw,
    # then clean) and one per output dataset (combine the cleaned sources).
    dataset_level_ops = CREATING_OPS | {TransformOp.BUILD_DIMENSION}
    source_tables = sorted(frames)
    for table in source_tables:
        steps_for_table = [
            st for st in plan.ordered_steps()
            if st.target_table == table and st.id not in execution.skipped_steps
            and st.op not in dataset_level_ops
        ]
        execution.m_queries[table] = build_source_query(
            table, f"raw/{table}.csv", raw_columns[table], steps_for_table,
            skipped=[
                f"{st.title or st.op.value} "
                f"({'rejected' if st.approved is False else 'not approved'})"
                for st in plan.ordered_steps()
                if st.target_table == table and st.id in execution.skipped_steps
            ],
        )

    for name, frame in outputs.items():
        buffer = io.StringIO()
        frame.to_csv(buffer, index=False)
        data = buffer.getvalue().encode("utf-8")
        key = project_key(project_id, "datasets", f"{name}.csv")
        store.put_bytes(key, data)

        from .. import kpi_catalog
        from ...schemas.common import CanonicalType
        from ..profiling.inference import classify_column

        columns: list[dict[str, Any]] = []
        for col in frame.columns:
            populated = [v for v in frame[col].tolist()[:2000]]
            verdict = classify_column(populated, str(col))
            non_null = frame[col].dropna()
            columns.append({
                "name": str(col),
                "canonical_type": verdict.canonical.value,
                "nullable": bool(frame[col].isna().any()),
                "role": kpi_catalog.role_for_column(str(col)),
                # Uniqueness decides whether this column can be a primary key.
                # Without it the modeller would hand a fact table a key it does
                # not have, which then looks like a valid one-side of a
                # relationship.
                "unique": bool(
                    len(frame) > 0 and len(non_null) == len(frame)
                    and non_null.astype(str).nunique() == len(frame)
                ),
                "distinct_count": int(non_null.astype(str).nunique()) if len(non_null) else 0,
            })

        dataset = PreparedDataset(
            project_id=project_id, plan_id=plan.id, name=name,
            stored_path=key, row_count=int(len(frame)), column_count=int(len(frame.columns)),
            columns=columns,
            derived_from_tables=sorted({
                e.sources[0].split(".")[0] for e in ctx.lineage
                if e.target.startswith(f"{name}.") and e.sources
            }),
            role_hint=_role_hint(name, frame),
        )
        execution.datasets.append(dataset)
        repo.save_dataset(s, dataset)

        if name in frames:
            # This dataset *is* a cleaned source table; its source-layer query
            # above already is the dataset query.
            continue
        steps_for_dataset = [
            st for st in plan.ordered_steps()
            if st.target_table == name and st.id not in execution.skipped_steps
        ]
        contributing: list[str] = []
        for st in steps_for_dataset:
            for t in st.params.get("tables", []) or []:
                if t not in contributing:
                    contributing.append(t)
            for ref in st.params.get("from_columns", []) or []:
                t = ref.split(".", 1)[0]
                if t not in contributing:
                    contributing.append(t)
        execution.m_queries[name] = build_dataset_query(
            name, steps_for_dataset, source_queries=contributing,
            skipped=[
                f"{st.title or st.op.value} "
                f"({'rejected' if st.approved is False else 'not approved'})"
                for st in plan.ordered_steps()
                if st.target_table == name and st.id in execution.skipped_steps
            ],
        )

    execution.succeeded = True
    execution.finished_at = utcnow()

    summary = compare(execution.quality_before, execution.quality_after)
    execution.log.append(
        f"Quality score {summary['score_before']} -> {summary['score_after']} "
        f"(delta {summary['delta']:+}). Rows in comparable tables "
        f"{summary['rows_before']:,} -> {summary['rows_after']:,}"
        + (f"; generated in addition: " + ", ".join(
            f'{k} ({v:,} rows)' for k, v in summary['generated_tables'].items()
          ) if summary.get('generated_tables') else "")
        + "."
    )
    if summary["regressed"]:
        execution.log.append(
            f"{len(summary['regressed'])} metric(s) got worse: "
            + "; ".join(summary["regressed"][:5])
        )

    repo.record_audit(
        s, project_id=project_id, actor=actor,
        action=AuditAction.TRANSFORMATION_EXECUTED, target=execution.id,
        summary=f"Executed {len(execution.row_count_deltas)} step(s), skipped "
                f"{len(execution.skipped_steps)}, produced "
                f"{len(execution.datasets)} dataset(s).",
        detail={"quality": summary, "datasets": [d.name for d in execution.datasets]},
    )
    for name, m in execution.m_queries.items():
        repo.record_audit(
            s, project_id=project_id, actor=actor, action=AuditAction.CODE_GENERATED,
            target=name, summary=f"Generated {len(m.splitlines())} lines of Power Query M "
                                 f"for '{name}'.",
        )
    return execution


def load_prepared_frames(s: Session, project_id: str) -> dict[str, pd.DataFrame]:
    """Read the prepared datasets back, for the model and DAX stages."""
    store = get_store()
    out: dict[str, pd.DataFrame] = {}
    for payload in repo.list_datasets(s, project_id):
        name = payload["name"]
        if name in out:
            continue
        try:
            data = store.get_bytes(payload["stored_path"])
        except FileNotFoundError:
            continue
        out[name] = pd.read_csv(io.BytesIO(data), dtype=object, keep_default_na=False,
                                na_values=[""])
    return out
