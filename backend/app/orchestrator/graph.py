"""The per-project task dependency graph."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..schemas.agent import (
    TASK_DEPENDENCIES, TASK_OWNER, AgentTask, TaskGraph, TaskKind, TaskStatus,
)
from ..services import repository as repo


def ensure_task_graph(s: Session, project_id: str) -> TaskGraph:
    """Create the standard graph for a project if it has none.

    Dependencies are stored as concrete task ids (not kinds) so a QA-triggered
    rework task can be inserted at runtime with its own edges.
    """
    existing = repo.load_task_graph(s, project_id)
    if existing.tasks:
        return existing

    by_kind: dict[TaskKind, AgentTask] = {}
    # Insert in dependency order so every parent id exists when referenced.
    for kind in _topological_order():
        task = AgentTask(
            project_id=project_id, kind=kind, owner=TASK_OWNER[kind],
            status=TaskStatus.PENDING,
        )
        by_kind[kind] = task
    for kind, task in by_kind.items():
        task.depends_on = [by_kind[d].id for d in TASK_DEPENDENCIES[kind]]
        task.status = TaskStatus.READY if not task.depends_on else TaskStatus.BLOCKED
        repo.save_task(s, task)
    return repo.load_task_graph(s, project_id)


def _topological_order() -> list[TaskKind]:
    order: list[TaskKind] = []
    seen: set[TaskKind] = set()

    def visit(k: TaskKind) -> None:
        if k in seen:
            return
        for d in TASK_DEPENDENCIES[k]:
            visit(d)
        seen.add(k)
        order.append(k)

    for k in TaskKind:
        visit(k)
    return order


def refresh_statuses(s: Session, project_id: str) -> TaskGraph:
    """Recompute BLOCKED/READY from dependency outcomes."""
    graph = repo.load_task_graph(s, project_id)
    done = {t.id for t in graph.tasks if t.status is TaskStatus.SUCCEEDED}
    failed = {t.id for t in graph.tasks if t.status is TaskStatus.FAILED}
    for t in graph.tasks:
        if t.status in {TaskStatus.BLOCKED, TaskStatus.READY, TaskStatus.PENDING}:
            if any(d in failed for d in t.depends_on):
                continue  # stays blocked; the failure must be addressed first
            new = TaskStatus.READY if all(d in done for d in t.depends_on) else TaskStatus.BLOCKED
            if new is not t.status:
                t.status = new
                repo.save_task(s, t)
    return repo.load_task_graph(s, project_id)


def add_rework_task(
    s: Session, project_id: str, kind: TaskKind, *, reason: str, requested_by: str = "qa_agent"
) -> AgentTask:
    """Insert a re-run of ``kind`` requested by QA, and re-block everything
    downstream of it so stale results are not treated as current."""
    graph = repo.load_task_graph(s, project_id)
    original = graph.by_kind(kind)
    task = AgentTask(
        project_id=project_id, kind=kind, owner=TASK_OWNER[kind],
        status=TaskStatus.READY, depends_on=list(original.depends_on) if original else [],
        requested_by=requested_by, rework_reason=reason,
    )
    repo.save_task(s, task)

    downstream = _downstream_kinds(kind)
    for t in graph.tasks:
        if t.kind in downstream and t.status is TaskStatus.SUCCEEDED:
            t.status = TaskStatus.BLOCKED
            t.depends_on = [d if d != (original.id if original else "") else task.id
                            for d in t.depends_on]
            if original and original.id in t.depends_on:
                t.depends_on = [task.id if d == original.id else d for d in t.depends_on]
            repo.save_task(s, t)
    return task


def _downstream_kinds(kind: TaskKind) -> set[TaskKind]:
    out: set[TaskKind] = set()

    def visit(k: TaskKind) -> None:
        for other, deps in TASK_DEPENDENCIES.items():
            if k in deps and other not in out:
                out.add(other)
                visit(other)

    visit(kind)
    return out
