"""The delivery page and file downloads."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response

from ..schemas.agent import AuditAction
from ..services import repository as repo
from ..services.auth import authorise_project
from ..services.capabilities import capabilities
from ..services.delivery import build_delivery_bundle
from ..services.storage import get_store
from .deps import Depends, Principal, Session, current_principal, get_session

router = APIRouter(prefix="/projects/{project_id}", tags=["deliverables"])


@router.get("/delivery")
def delivery(
    project_id: str,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    """Everything the project has produced, with each section marked available
    or unavailable-with-a-reason. Never reports a deliverable that does not
    exist on disk."""
    authorise_project(s, principal, project_id)
    bundle = build_delivery_bundle(s, project_id)
    return bundle.model_dump(mode="json")


@router.get("/exports")
def list_exports(
    project_id: str,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    authorise_project(s, principal, project_id)
    store = get_store()
    out = []
    for art in repo.list_exports(s, project_id):
        payload = art.model_dump(mode="json")
        # Report presence from the filesystem, not from the database row.
        payload["file_present"] = store.exists(art.stored_path)
        out.append(payload)
    return {
        "exports": out,
        "capabilities": [
            c.model_dump(mode="json") for c in capabilities()
            if c.key.startswith(("export.", "model.", "report."))
        ],
    }


@router.get("/exports/{export_id}/download")
def download_export(
    project_id: str,
    export_id: str,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> Response:
    authorise_project(s, principal, project_id)
    art = repo.get_export(s, export_id)
    if art is None or art.project_id != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Export not found.")
    store = get_store()
    if not store.exists(art.stored_path):
        raise HTTPException(
            status.HTTP_410_GONE,
            "The export file is no longer present on disk. Re-run the export task.",
        )
    data = store.get_bytes(art.stored_path)
    repo.record_audit(
        s, project_id=project_id, actor=principal.user_id,
        action=AuditAction.EXPORT_DOWNLOADED, target=export_id,
        summary=f"Downloaded '{art.filename}' ({len(data):,} bytes).",
    )
    media = "application/zip" if art.filename.endswith(".zip") else "application/octet-stream"
    if art.filename.endswith(".md"):
        media = "text/markdown; charset=utf-8"
    return Response(
        content=data, media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{art.filename}"',
            "Content-Length": str(len(data)),
        },
    )


@router.get("/minutes.md")
def minutes_markdown(
    project_id: str,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> Response:
    """The Minutes of Meeting as Markdown, ready to circulate."""
    authorise_project(s, principal, project_id)
    from ..schemas.requirements import MeetingMinutes
    from ..services.mom import render_minutes

    minutes = repo.load_artifact(s, project_id, repo.ArtifactKind.MINUTES, MeetingMinutes)
    if minutes is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No minutes exist for this project. Attach a meeting transcript and run the "
            "requirements agent.",
        )
    return Response(
        content=render_minutes(minutes), media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="minutes_of_meeting.md"'},
    )
