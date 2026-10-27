"""Persistence helpers over the tables in ``app.db``.

Artifacts are stored as versioned JSON documents. ``save_artifact`` always
writes a *new version* and demotes the previous one, which gives us the
versioning of model and report artifacts the brief asks for, and makes the
audit trail honest: nothing is overwritten in place.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, TypeVar

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..db import (
    ApprovalRow, ArtifactRow, AuditRow, DatasetRow, ExecutionRow, ExportRow,
    ProjectRow, SourceFileRow, TaskRow, UserRow,
)
from ..schemas.agent import (
    AgentExecution, AgentTask, ApprovalRequest, ApprovalState, AuditAction, AuditEvent,
    TaskGraph, TaskKind, TaskStatus,
)
from ..schemas.common import Contract, Severity, new_id
from ..schemas.project import Project, ProjectStatus
from ..schemas.report import ExportArtifact
from ..schemas.source import SourceFile

T = TypeVar("T", bound=Contract)


class ArtifactKind:
    """String keys for the ``artifacts`` table. Kept as constants so a typo
    cannot silently create a new artifact stream."""

    PROFILE = "source_data_profile"
    MINUTES = "meeting_minutes"
    REQUIREMENTS = "requirements_spec"
    PLAN = "transformation_plan"
    EXECUTION = "transformation_execution"
    MODEL = "semantic_model"
    REPORT = "report_spec"
    VALIDATION = "validation_report"
    IMPROVEMENT = "improvement_report"
    QUALITY_BEFORE = "quality_before"
    QUALITY_AFTER = "quality_after"

    ALL = (
        PROFILE, MINUTES, REQUIREMENTS, PLAN, EXECUTION, MODEL, REPORT,
        VALIDATION, IMPROVEMENT, QUALITY_BEFORE, QUALITY_AFTER,
    )


def _dump(model: Contract) -> dict[str, Any]:
    # mode="json" so datetimes/enums round-trip through JSON columns cleanly.
    return json.loads(model.model_dump_json())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
def create_user(
    s: Session, *, email: str, display_name: str = "", password_hash: str = "",
    tenant_id: str = "default", role: str = "member", user_id: str | None = None,
) -> UserRow:
    row = UserRow(
        id=user_id or new_id("usr"), email=email.lower().strip(),
        display_name=display_name or email.split("@")[0],
        password_hash=password_hash, tenant_id=tenant_id, role=role,
    )
    s.add(row)
    s.flush()
    return row


def get_user_by_email(s: Session, email: str) -> UserRow | None:
    return s.scalar(select(UserRow).where(UserRow.email == email.lower().strip()))


def get_user(s: Session, user_id: str) -> UserRow | None:
    return s.get(UserRow, user_id)


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------
def save_project(s: Session, project: Project) -> Project:
    project.updated_at = _now()
    row = s.get(ProjectRow, project.id)
    payload = _dump(project)
    if row is None:
        s.add(ProjectRow(
            id=project.id, tenant_id=project.tenant_id, owner_id=project.owner_id,
            name=project.name, status=project.status.value, payload=payload,
        ))
    else:
        row.name = project.name
        row.status = project.status.value
        row.payload = payload
        row.updated_at = project.updated_at
    s.flush()
    return project


def get_project(s: Session, project_id: str) -> Project | None:
    row = s.get(ProjectRow, project_id)
    return Project.model_validate(row.payload) if row else None


def list_projects(
    s: Session, *, owner_id: str | None = None, tenant_id: str | None = None, limit: int = 100
) -> list[Project]:
    stmt = select(ProjectRow).order_by(ProjectRow.updated_at.desc()).limit(limit)
    if owner_id:
        stmt = stmt.where(ProjectRow.owner_id == owner_id)
    if tenant_id:
        stmt = stmt.where(ProjectRow.tenant_id == tenant_id)
    return [Project.model_validate(r.payload) for r in s.scalars(stmt)]


def set_project_status(s: Session, project_id: str, status: ProjectStatus) -> None:
    p = get_project(s, project_id)
    if p:
        p.status = status
        save_project(s, p)


def delete_project(s: Session, project_id: str) -> None:
    s.execute(delete(ProjectRow).where(ProjectRow.id == project_id))


# --------------------------------------------------------------------------
# Source files
# --------------------------------------------------------------------------
def save_source_file(s: Session, f: SourceFile) -> SourceFile:
    row = s.get(SourceFileRow, f.id)
    payload = _dump(f)
    if row is None:
        s.add(SourceFileRow(
            id=f.id, project_id=f.project_id, original_filename=f.original_filename,
            kind=f.kind.value, status=f.status.value, sha256=f.sha256,
            size_bytes=f.size_bytes, payload=payload,
        ))
    else:
        row.status = f.status.value
        row.payload = payload
    s.flush()
    return f


def list_source_files(s: Session, project_id: str) -> list[SourceFile]:
    rows = s.scalars(
        select(SourceFileRow)
        .where(SourceFileRow.project_id == project_id)
        .order_by(SourceFileRow.created_at)
    )
    return [SourceFile.model_validate(r.payload) for r in rows]


def get_source_file(s: Session, file_id: str) -> SourceFile | None:
    row = s.get(SourceFileRow, file_id)
    return SourceFile.model_validate(row.payload) if row else None


def find_duplicate_upload(s: Session, project_id: str, sha256: str) -> SourceFile | None:
    row = s.scalar(
        select(SourceFileRow).where(
            SourceFileRow.project_id == project_id, SourceFileRow.sha256 == sha256
        )
    )
    return SourceFile.model_validate(row.payload) if row else None


def count_source_files(s: Session, project_id: str) -> int:
    return int(s.scalar(
        select(func.count()).select_from(SourceFileRow)
        .where(SourceFileRow.project_id == project_id)
    ) or 0)


# --------------------------------------------------------------------------
# Artifacts (versioned)
# --------------------------------------------------------------------------
def save_artifact(s: Session, project_id: str, kind: str, model: Contract) -> str:
    """Write a new version of an artifact and make it current."""
    if kind not in ArtifactKind.ALL:
        raise ValueError(f"unknown artifact kind: {kind!r}")
    current_max = s.scalar(
        select(func.max(ArtifactRow.version))
        .where(ArtifactRow.project_id == project_id, ArtifactRow.kind == kind)
    ) or 0
    s.query(ArtifactRow).filter(
        ArtifactRow.project_id == project_id,
        ArtifactRow.kind == kind,
        ArtifactRow.is_current.is_(True),
    ).update({"is_current": False})
    artifact_id = getattr(model, "id", None) or new_id("art")
    s.add(ArtifactRow(
        id=new_id("ver"), artifact_id=artifact_id, project_id=project_id, kind=kind,
        version=current_max + 1, is_current=True, payload=_dump(model),
    ))
    s.flush()
    return artifact_id


def load_artifact(
    s: Session, project_id: str, kind: str, model_cls: type[T],
    *, artifact_id: str = "", version: int | None = None,
) -> T | None:
    stmt = select(ArtifactRow).where(
        ArtifactRow.project_id == project_id, ArtifactRow.kind == kind
    )
    if artifact_id:
        stmt = stmt.where(ArtifactRow.artifact_id == artifact_id)
    elif version is not None:
        stmt = stmt.where(ArtifactRow.version == version)
    else:
        stmt = stmt.where(ArtifactRow.is_current.is_(True))
    row = s.scalar(stmt.order_by(ArtifactRow.version.desc()).limit(1))
    return model_cls.model_validate(row.payload) if row else None


def artifact_versions(s: Session, project_id: str, kind: str) -> list[dict[str, Any]]:
    rows = s.scalars(
        select(ArtifactRow)
        .where(ArtifactRow.project_id == project_id, ArtifactRow.kind == kind)
        .order_by(ArtifactRow.version.desc())
    )
    return [
        {"id": r.artifact_id, "version_row_id": r.id, "version": r.version,
         "is_current": r.is_current,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows
    ]


# --------------------------------------------------------------------------
# Datasets
# --------------------------------------------------------------------------
def save_dataset(s: Session, ds: Contract) -> None:
    payload = _dump(ds)
    row = s.get(DatasetRow, payload["id"])
    if row is None:
        s.add(DatasetRow(
            id=payload["id"], project_id=payload["project_id"], name=payload["name"],
            version=payload.get("version", 1), stored_path=payload["stored_path"],
            row_count=payload.get("row_count", 0), payload=payload,
        ))
    else:
        row.payload = payload
        row.row_count = payload.get("row_count", 0)
        row.stored_path = payload["stored_path"]
    s.flush()


def list_datasets(s: Session, project_id: str) -> list[dict[str, Any]]:
    rows = s.scalars(
        select(DatasetRow).where(DatasetRow.project_id == project_id)
        .order_by(DatasetRow.name, DatasetRow.version.desc())
    )
    return [r.payload for r in rows]


def get_dataset(s: Session, project_id: str, name: str) -> dict[str, Any] | None:
    row = s.scalar(
        select(DatasetRow)
        .where(DatasetRow.project_id == project_id, DatasetRow.name == name)
        .order_by(DatasetRow.version.desc()).limit(1)
    )
    return row.payload if row else None


# --------------------------------------------------------------------------
# Tasks and executions
# --------------------------------------------------------------------------
def save_task(s: Session, task: AgentTask) -> AgentTask:
    task.updated_at = _now()
    row = s.get(TaskRow, task.id)
    payload = _dump(task)
    if row is None:
        s.add(TaskRow(
            id=task.id, project_id=task.project_id, kind=task.kind.value,
            owner=task.owner.value, status=task.status.value, payload=payload,
        ))
    else:
        row.status = task.status.value
        row.payload = payload
        row.updated_at = task.updated_at
    s.flush()
    return task


def load_task_graph(s: Session, project_id: str) -> TaskGraph:
    rows = s.scalars(
        select(TaskRow).where(TaskRow.project_id == project_id).order_by(TaskRow.created_at)
    )
    return TaskGraph(project_id=project_id, tasks=[AgentTask.model_validate(r.payload) for r in rows])


def get_task(s: Session, task_id: str) -> AgentTask | None:
    row = s.get(TaskRow, task_id)
    return AgentTask.model_validate(row.payload) if row else None


def find_task(s: Session, project_id: str, kind: TaskKind) -> AgentTask | None:
    row = s.scalar(
        select(TaskRow)
        .where(TaskRow.project_id == project_id, TaskRow.kind == kind.value)
        .order_by(TaskRow.created_at.desc()).limit(1)
    )
    return AgentTask.model_validate(row.payload) if row else None


def save_execution(s: Session, ex: AgentExecution) -> AgentExecution:
    row = s.get(ExecutionRow, ex.id)
    payload = _dump(ex)
    if row is None:
        s.add(ExecutionRow(
            id=ex.id, project_id=ex.project_id, task_id=ex.task_id,
            agent=ex.agent.value, status=ex.status.value,
            cost_usd=ex.cost.amount_usd, duration_ms=ex.duration_ms, payload=payload,
        ))
    else:
        row.status = ex.status.value
        row.cost_usd = ex.cost.amount_usd
        row.duration_ms = ex.duration_ms
        row.payload = payload
    s.flush()
    return ex


def list_executions(
    s: Session, *, project_id: str | None = None, limit: int = 50
) -> list[AgentExecution]:
    stmt = select(ExecutionRow).order_by(ExecutionRow.created_at.desc()).limit(limit)
    if project_id:
        stmt = stmt.where(ExecutionRow.project_id == project_id)
    return [AgentExecution.model_validate(r.payload) for r in s.scalars(stmt)]


def project_spend_usd(s: Session, project_id: str) -> float:
    return float(s.scalar(
        select(func.coalesce(func.sum(ExecutionRow.cost_usd), 0.0))
        .where(ExecutionRow.project_id == project_id)
    ) or 0.0)


# --------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------
def save_approval(s: Session, req: ApprovalRequest) -> ApprovalRequest:
    row = s.get(ApprovalRow, req.id)
    payload = _dump(req)
    if row is None:
        s.add(ApprovalRow(
            id=req.id, project_id=req.project_id, kind=req.kind.value,
            subject_id=req.subject_id, state=req.state.value, payload=payload,
        ))
    else:
        row.state = req.state.value
        row.payload = payload
    s.flush()
    return req


def list_approvals(
    s: Session, project_id: str, *, state: ApprovalState | None = None
) -> list[ApprovalRequest]:
    stmt = select(ApprovalRow).where(ApprovalRow.project_id == project_id)
    if state:
        stmt = stmt.where(ApprovalRow.state == state.value)
    return [
        ApprovalRequest.model_validate(r.payload)
        for r in s.scalars(stmt.order_by(ApprovalRow.created_at))
    ]


def get_approval(s: Session, approval_id: str) -> ApprovalRequest | None:
    row = s.get(ApprovalRow, approval_id)
    return ApprovalRequest.model_validate(row.payload) if row else None


def find_approval(
    s: Session, project_id: str, subject_id: str
) -> ApprovalRequest | None:
    row = s.scalar(
        select(ApprovalRow).where(
            ApprovalRow.project_id == project_id, ApprovalRow.subject_id == subject_id
        ).order_by(ApprovalRow.created_at.desc()).limit(1)
    )
    return ApprovalRequest.model_validate(row.payload) if row else None


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------
def record_audit(
    s: Session, *, project_id: str = "", actor: str, action: AuditAction,
    summary: str = "", target: str = "", detail: dict[str, Any] | None = None,
    severity: Severity = Severity.INFO, execution_id: str = "",
) -> AuditEvent:
    ev = AuditEvent(
        project_id=project_id, actor=actor, action=action, summary=summary,
        target=target, detail=detail or {}, severity=severity, execution_id=execution_id,
    )
    s.add(AuditRow(
        id=ev.id, project_id=ev.project_id, actor=ev.actor, action=ev.action.value,
        severity=ev.severity.value, summary=ev.summary, payload=_dump(ev), at=ev.at,
    ))
    s.flush()
    return ev


def record_denial(
    s: Session, *, project_id: str = "", actor: str, action: AuditAction,
    summary: str = "", target: str = "", detail: dict[str, Any] | None = None,
    severity: Severity = Severity.MEDIUM,
) -> AuditEvent:
    """Record a refused access so that it survives the request being aborted.

    A handler that denies raises, and the request's transaction is then rolled
    back -- discarding any audit row written inside it. Every refused access
    was invisible in the trail for exactly that reason: failed sign-ins,
    cross-tenant attempts, closed registration. Those are the events a security
    review most wants.

    Writing from a second session deadlocks on SQLite, which serialises
    writers. So this rolls the caller's transaction back first -- safe, because
    the handler is about to abort it anyway -- and then commits the audit row
    on that same connection. What the request was doing is discarded; the fact
    that it was denied is kept.
    """
    s.rollback()
    event = record_audit(
        s, project_id=project_id, actor=actor, action=action, summary=summary,
        target=target, detail=detail, severity=severity,
    )
    s.commit()
    return event


def list_audit(
    s: Session, *, project_id: str | None = None, limit: int = 200,
    actions: list[str] | None = None, offset: int = 0,
) -> list[AuditEvent]:
    stmt = select(AuditRow).order_by(AuditRow.at.desc()).offset(offset).limit(limit)
    if project_id:
        stmt = stmt.where(AuditRow.project_id == project_id)
    if actions:
        stmt = stmt.where(AuditRow.action.in_(actions))
    return [AuditEvent.model_validate(r.payload) for r in s.scalars(stmt)]


def count_audit(s: Session, project_id: str | None = None) -> int:
    stmt = select(func.count()).select_from(AuditRow)
    if project_id:
        stmt = stmt.where(AuditRow.project_id == project_id)
    return int(s.scalar(stmt) or 0)


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------
def save_export(s: Session, art: ExportArtifact) -> ExportArtifact:
    row = s.get(ExportRow, art.id)
    payload = _dump(art)
    if row is None:
        s.add(ExportRow(
            id=art.id, project_id=art.project_id, kind=art.kind.value,
            filename=art.filename, stored_path=art.stored_path,
            size_bytes=art.size_bytes, payload=payload,
        ))
    else:
        row.payload = payload
    s.flush()
    return art


def list_exports(s: Session, project_id: str) -> list[ExportArtifact]:
    rows = s.scalars(
        select(ExportRow).where(ExportRow.project_id == project_id)
        .order_by(ExportRow.created_at.desc())
    )
    return [ExportArtifact.model_validate(r.payload) for r in rows]


def get_export(s: Session, export_id: str) -> ExportArtifact | None:
    row = s.get(ExportRow, export_id)
    return ExportArtifact.model_validate(row.payload) if row else None
