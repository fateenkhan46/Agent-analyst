"""Cross-project dashboard data."""
from __future__ import annotations

from fastapi import APIRouter

from ..schemas.agent import ExecutionStatus
from ..schemas.qa import ValidationReport
from ..services import repository as repo
from ..services.capabilities import environment_summary
from .deps import Depends, Principal, Session, current_principal, get_session

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
def dashboard(
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    projects = repo.list_projects(s, owner_id=principal.user_id, tenant_id=principal.tenant_id)
    recent_projects, total_spend, failures = [], 0.0, []
    for p in projects[:12]:
        graph = repo.load_task_graph(s, p.id)
        spend = repo.project_spend_usd(s, p.id)
        total_spend += spend
        vr = repo.load_artifact(s, p.id, repo.ArtifactKind.VALIDATION, ValidationReport)
        if vr:
            for r in vr.blocking_failures:
                failures.append({
                    "project_id": p.id, "project_name": p.name,
                    "test": r.test_name, "target": r.target, "actual": r.actual,
                    "severity": r.severity.value, "fix": r.recommended_fix,
                })
        recent_projects.append({
            "id": p.id, "name": p.name, "status": p.status.value,
            "updated_at": p.updated_at.isoformat(), "progress": graph.progress,
            "file_count": repo.count_source_files(s, p.id),
            "spend_usd": round(spend, 6),
            "qa_passed": vr.passed if vr else None,
        })

    executions = repo.list_executions(s, limit=15)
    return {
        "projects": recent_projects,
        "project_count": len(projects),
        "recent_executions": [
            {
                "id": e.id, "project_id": e.project_id, "agent": e.agent.value,
                "status": e.status.value, "duration_ms": e.duration_ms,
                "cost_usd": e.cost.amount_usd, "started_at": e.started_at.isoformat(),
                "error": e.error[:300],
            }
            for e in executions
        ],
        "validation_failures": failures[:25],
        "spend": {
            "total_usd": round(total_spend, 6),
            "note": environment_summary()["llm_provider_active"] == "heuristic"
            and "No LLM is configured, so AI cost is $0.00. Figures become real once "
                "ANTHROPIC_API_KEY is set." or "",
        },
        "failed_executions": sum(
            1 for e in executions if e.status is not ExecutionStatus.SUCCEEDED
        ),
        "environment": environment_summary(),
    }
