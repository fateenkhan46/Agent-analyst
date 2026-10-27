"""Running agents, approving their proposals, and reading execution history."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, status

from ..orchestrator.engine import (
    OrchestrationBlocked, decide_approval, run_pipeline, run_task,
)
from ..orchestrator.graph import ensure_task_graph, refresh_statuses
from ..schemas.agent import ApprovalState, TaskKind
from ..services import repository as repo
from ..services.auth import authorise_project
from .deps import Depends, Principal, Session, current_principal, get_session

router = APIRouter(prefix="/projects/{project_id}", tags=["orchestration"])


@router.get("/tasks")
def get_tasks(
    project_id: str,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    authorise_project(s, principal, project_id)
    ensure_task_graph(s, project_id)
    graph = refresh_statuses(s, project_id)
    return {
        "tasks": [t.model_dump(mode="json") for t in graph.tasks],
        "progress": graph.progress,
        "runnable": [t.kind.value for t in graph.runnable()],
    }


@router.post("/tasks/{kind}/run")
def run_single_task(
    project_id: str,
    kind: str,
    payload: dict = Body(default_factory=dict),
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    authorise_project(s, principal, project_id)
    try:
        task_kind = TaskKind(kind)
    except ValueError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown task '{kind}'. Valid: {', '.join(k.value for k in TaskKind)}.",
        )
    try:
        return run_task(s, project_id, task_kind, actor=principal.user_id, extra_payload=payload)
    except OrchestrationBlocked as exc:
        return {
            "succeeded": False, "blocked": True, "reason": exc.reason,
            "approvals": [a.model_dump(mode="json") for a in exc.approvals],
        }
    except KeyError as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc))


@router.post("/run")
def run_whole_pipeline(
    project_id: str,
    payload: dict = Body(default_factory=dict),
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    """Drive the graph as far as it will go. Stops at the first block."""
    authorise_project(s, principal, project_id)
    stop_after = payload.get("stop_after")
    kind = None
    if stop_after:
        try:
            kind = TaskKind(stop_after)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown task '{stop_after}'.")
    steps = run_pipeline(
        s, project_id, actor=principal.user_id, stop_after=kind,
        auto_approve=bool(payload.get("auto_approve")),
    )
    graph = refresh_statuses(s, project_id)
    return {
        "steps": steps, "progress": graph.progress,
        "pending_approvals": [
            a.model_dump(mode="json")
            for a in repo.list_approvals(s, project_id, state=ApprovalState.PENDING)
        ],
    }


@router.get("/approvals")
def get_approvals(
    project_id: str,
    state: str | None = None,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    authorise_project(s, principal, project_id)
    st = ApprovalState(state) if state else None
    return {
        "approvals": [a.model_dump(mode="json") for a in repo.list_approvals(s, project_id, state=st)]
    }


@router.post("/approvals/{approval_id}")
def decide(
    project_id: str,
    approval_id: str,
    payload: dict = Body(...),
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    authorise_project(s, principal, project_id)
    if "approve" not in payload:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must include 'approve': true|false.")
    try:
        req = decide_approval(
            s, project_id, approval_id, approve=bool(payload["approve"]),
            decided_by=principal.user_id, note=str(payload.get("note", "")),
        )
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval request not found.")
    return {"approval": req.model_dump(mode="json")}


@router.get("/executions")
def get_executions(
    project_id: str,
    limit: int = 50,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    authorise_project(s, principal, project_id)
    return {
        "executions": [
            e.model_dump(mode="json")
            for e in repo.list_executions(s, project_id=project_id, limit=min(limit, 200))
        ]
    }
