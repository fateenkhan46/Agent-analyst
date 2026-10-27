"""Project lifecycle, uploads, profiling and previews."""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from ..config import get_settings
from ..schemas.agent import AuditAction
from ..schemas.project import Project, ProjectCreate, ProjectStatus
from ..schemas.source import SourceDataProfile
from ..services import repository as repo
from ..services.auth import authorise_project
from ..services.profiling.service import (
    UploadRejected, ingest_upload, preview_table, profile_project,
)
from .deps import Depends, Principal, Session, current_principal, get_session

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_project(
    body: ProjectCreate,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    settings = get_settings()
    project = Project(
        owner_id=principal.user_id, tenant_id=principal.tenant_id,
        name=body.name.strip(), description=body.description,
        business_objective=body.business_objective,
        desired_kpis=[k for k in body.desired_kpis if k.strip()],
        reporting_questions=[q for q in body.reporting_questions if q.strip()],
        reporting_period=body.reporting_period, currency=body.currency, units=body.units,
        business_definitions=body.business_definitions,
        design_preferences=body.design_preferences,
        budget_usd=settings.default_project_budget_usd,
    )
    project.workspace_path = f"{project.id}/"
    repo.save_project(s, project)
    repo.record_audit(
        s, project_id=project.id, actor=principal.user_id,
        action=AuditAction.PROJECT_CREATED,
        summary=f"Created project '{project.name}'.", target=project.id,
    )

    # A transcript pasted at creation is stored as a first-class source file so
    # it is versioned and auditable like any other input, and so the MoM can
    # cite line numbers against something immutable.
    if body.meeting_transcript.strip():
        ingest_upload(
            s, project_id=project.id, filename="meeting_transcript.md",
            data=body.meeting_transcript.encode("utf-8"),
            content_type="text/markdown", actor=principal.user_id,
        )

    from ..orchestrator.graph import ensure_task_graph
    ensure_task_graph(s, project.id)
    return {"project": project.model_dump(mode="json")}


@router.get("")
def list_projects(
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    projects = repo.list_projects(s, owner_id=principal.user_id, tenant_id=principal.tenant_id)
    out = []
    for p in projects:
        graph = repo.load_task_graph(s, p.id)
        out.append({
            **p.model_dump(mode="json"),
            "file_count": repo.count_source_files(s, p.id),
            "progress": graph.progress,
            "spend_usd": round(repo.project_spend_usd(s, p.id), 6),
        })
    return {"projects": out}


@router.get("/{project_id}")
def get_project(
    project_id: str,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    project = authorise_project(s, principal, project_id)
    graph = repo.load_task_graph(s, project_id)
    return {
        "project": project.model_dump(mode="json"),
        "files": [f.model_dump(mode="json") for f in repo.list_source_files(s, project_id)],
        "tasks": [t.model_dump(mode="json") for t in graph.tasks],
        "progress": graph.progress,
        "approvals_pending": [
            a.model_dump(mode="json") for a in repo.list_approvals(s, project_id)
            if a.state.value == "pending"
        ],
        "spend_usd": round(repo.project_spend_usd(s, project_id), 6),
    }


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: str,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> None:
    authorise_project(s, principal, project_id)
    from ..services.storage import get_store
    get_store().delete_prefix(project_id)
    repo.delete_project(s, project_id)


@router.post("/{project_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_files(
    project_id: str,
    files: list[UploadFile] = File(...),
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    """Upload one or more sources. A rejected file is recorded with its reason
    rather than aborting the whole batch."""
    authorise_project(s, principal, project_id)
    settings = get_settings()
    accepted, rejected = [], []
    for upload in files:
        data = await upload.read()
        if len(data) > settings.max_upload_bytes:
            rejected.append({
                "filename": upload.filename or "",
                "reason": f"Larger than the {settings.max_upload_bytes // (1024*1024)} MB limit.",
            })
            continue
        try:
            sf = ingest_upload(
                s, project_id=project_id, filename=upload.filename or "upload",
                data=data, content_type=upload.content_type or "",
                actor=principal.user_id,
            )
        except UploadRejected as exc:
            rejected.append({"filename": upload.filename or "", "reason": exc.reason})
            continue
        if sf.status.value == "rejected":
            rejected.append({"filename": sf.original_filename, "reason": sf.rejection_reason})
        else:
            accepted.append(sf.model_dump(mode="json"))
    return {"accepted": accepted, "rejected": rejected}


@router.post("/{project_id}/transcript", status_code=status.HTTP_201_CREATED)
def add_transcript(
    project_id: str,
    text: str = Form(...),
    filename: str = Form("meeting_transcript.md"),
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    """Attach a meeting transcript as text. Feeds the MoM and requirements."""
    authorise_project(s, principal, project_id)
    if not text.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The transcript is empty.")
    sf = ingest_upload(
        s, project_id=project_id, filename=filename or "meeting_transcript.md",
        data=text.encode("utf-8"), content_type="text/markdown", actor=principal.user_id,
    )
    return {"file": sf.model_dump(mode="json")}


@router.get("/{project_id}/files")
def list_files(
    project_id: str,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    authorise_project(s, principal, project_id)
    return {"files": [f.model_dump(mode="json") for f in repo.list_source_files(s, project_id)]}


@router.post("/{project_id}/profile")
def run_profile(
    project_id: str,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    """Profile every uploaded data source (Step 2)."""
    authorise_project(s, principal, project_id)
    from ..services.profiling.readers import DATA_KINDS
    files = [
        f for f in repo.list_source_files(s, project_id)
        if f.status.value != "rejected" and f.kind in DATA_KINDS
    ]
    if not files:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Upload at least one .csv or .xlsx data file before profiling. "
            "A transcript or brief on its own carries no data to profile.",
        )
    repo.set_project_status(s, project_id, ProjectStatus.PROFILING)
    profile = profile_project(s, project_id, actor=principal.user_id)
    repo.set_project_status(s, project_id, ProjectStatus.DRAFT)
    if not profile.tables:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No tabular data could be read from the uploaded files. "
            + "; ".join(i.message for i in profile.cross_table_issues[:3]),
        )
    return {"profile": profile.model_dump(mode="json")}


@router.get("/{project_id}/profile")
def get_profile(
    project_id: str,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    authorise_project(s, principal, project_id)
    profile = repo.load_artifact(s, project_id, repo.ArtifactKind.PROFILE, SourceDataProfile)
    if profile is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "This project has not been profiled yet. POST to /profile first.",
        )
    return {
        "profile": profile.model_dump(mode="json"),
        "issue_counts": profile.issue_counts(),
        "versions": repo.artifact_versions(s, project_id, repo.ArtifactKind.PROFILE),
    }


@router.get("/{project_id}/tables/{table_name}/rows")
def get_table_rows(
    project_id: str,
    table_name: str,
    limit: int = 100,
    offset: int = 0,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    authorise_project(s, principal, project_id)
    try:
        return preview_table(
            s, project_id, table_name, limit=min(max(limit, 1), 500), offset=max(offset, 0)
        )
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No table named '{table_name}'.")


@router.get("/{project_id}/audit")
def get_audit(
    project_id: str,
    limit: int = 200,
    offset: int = 0,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    authorise_project(s, principal, project_id)
    return {
        "events": [
            e.model_dump(mode="json")
            for e in repo.list_audit(
                s, project_id=project_id, limit=min(max(limit, 1), 500), offset=max(offset, 0)
            )
        ],
        "total": repo.count_audit(s, project_id),
    }
