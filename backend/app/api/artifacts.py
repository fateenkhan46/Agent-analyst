"""Read access to every generated artifact, plus artifact-level edits.

These are the endpoints the Data Explorer, Data Model, DAX Studio, Report
Designer and QA pages read from. Each returns the stored contract verbatim
plus its version history, so the UI shows real state rather than a summary
the backend invented for it.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, status

from ..schemas.prep import TransformationExecution, TransformationPlan
from ..schemas.qa import ImprovementReport, ValidationReport
from ..schemas.report import ReportSpec
from ..schemas.requirements import MeetingMinutes, RequirementsSpec
from ..schemas.semantic import SemanticModel
from ..schemas.source import SourceDataProfile
from ..services import repository as repo
from ..services.auth import authorise_project
from .deps import Depends, Principal, Session, current_principal, get_session

router = APIRouter(prefix="/projects/{project_id}", tags=["artifacts"])

_KINDS = {
    "requirements": (repo.ArtifactKind.REQUIREMENTS, RequirementsSpec),
    "minutes": (repo.ArtifactKind.MINUTES, MeetingMinutes),
    "plan": (repo.ArtifactKind.PLAN, TransformationPlan),
    "execution": (repo.ArtifactKind.EXECUTION, TransformationExecution),
    "model": (repo.ArtifactKind.MODEL, SemanticModel),
    "report": (repo.ArtifactKind.REPORT, ReportSpec),
    "validation": (repo.ArtifactKind.VALIDATION, ValidationReport),
    "improvements": (repo.ArtifactKind.IMPROVEMENT, ImprovementReport),
}


def _load(s: Session, project_id: str, name: str, version: int | None = None):
    if name not in _KINDS:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Unknown artifact '{name}'. Available: {', '.join(sorted(_KINDS))}.",
        )
    kind, model_cls = _KINDS[name]
    art = repo.load_artifact(s, project_id, kind, model_cls, version=version)
    if art is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No '{name}' artifact exists for this project yet. Run the agent that "
            f"produces it first.",
        )
    return kind, art


@router.get("/artifacts")
def list_all(
    project_id: str,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    """Which artifacts exist, and at what version."""
    authorise_project(s, principal, project_id)
    out = {}
    for name, (kind, _) in {**_KINDS, "profile": (repo.ArtifactKind.PROFILE, None)}.items():
        versions = repo.artifact_versions(s, project_id, kind)
        out[name] = {"exists": bool(versions), "versions": versions}
    return {"artifacts": out}


@router.get("/artifacts/{name}")
def get_artifact(
    project_id: str,
    name: str,
    version: int | None = None,
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    authorise_project(s, principal, project_id)
    kind, art = _load(s, project_id, name, version)
    return {
        "artifact": art.model_dump(mode="json"),
        "versions": repo.artifact_versions(s, project_id, kind),
    }


# --------------------------------------------------------------------------
# Targeted edits -- a user correcting an agent
# --------------------------------------------------------------------------
@router.put("/measures/{measure_name}")
def edit_measure(
    project_id: str,
    measure_name: str,
    payload: dict = Body(...),
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    """Edit a measure's DAX by hand. The new expression is re-validated before
    it is stored, and the result of that validation is returned -- an invalid
    expression is saved only if ``force`` is set, and is then marked unapproved
    so QA still reports it."""
    authorise_project(s, principal, project_id)
    _, model = _load(s, project_id, "model")
    measure = model.measure(measure_name)
    if measure is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No measure named '{measure_name}'.")

    from ..services.dax.validator import validate_measure

    new_expression = str(payload.get("expression", measure.expression))
    probe = measure.model_copy(update={"expression": new_expression})
    results = validate_measure(probe, model)
    blocking = [r for r in results if r.status.is_blocking]
    if blocking and not payload.get("force"):
        return {
            "saved": False,
            "validation": [r.model_dump(mode="json") for r in results],
            "reason": "The expression did not validate; nothing was saved. "
                      "Re-send with force=true to store it anyway.",
        }

    measure.expression = new_expression
    if "description" in payload:
        measure.description = str(payload["description"])
    if "format_string" in payload:
        measure.format_string = str(payload["format_string"])
    measure.approved = None if blocking else True
    from ..schemas.common import Origin, Provenance
    measure.provenance = Provenance(
        origin=Origin.USER, produced_by=principal.user_id,
        rationale="Edited by hand in DAX Studio.",
    )
    model.version += 1
    repo.save_artifact(s, project_id, repo.ArtifactKind.MODEL, model)
    from ..schemas.agent import AuditAction
    repo.record_audit(
        s, project_id=project_id, actor=principal.user_id,
        action=AuditAction.CODE_GENERATED, target=measure.name,
        summary=f"Measure '{measure.name}' edited by hand.",
        detail={"expression": new_expression, "forced": bool(payload.get("force"))},
    )
    return {
        "saved": True, "measure": measure.model_dump(mode="json"),
        "validation": [r.model_dump(mode="json") for r in results],
    }


@router.post("/measures/{measure_name}/evaluate")
def evaluate_measure(
    project_id: str,
    measure_name: str,
    payload: dict = Body(default_factory=dict),
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    """Execute a measure against the prepared data via DuckDB, if the
    expression is inside the supported subset. Execution is read-only and is
    kept separate from persisting a measure."""
    authorise_project(s, principal, project_id)
    _, model = _load(s, project_id, "model")
    measure = model.measure(measure_name)
    if measure is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No measure named '{measure_name}'.")
    from ..services.dax.evaluator import evaluate

    filters = {str(k): str(v) for k, v in (payload.get("filters") or {}).items()}
    outcome = evaluate(s, project_id, measure, model, filters=filters)
    return outcome.model_dump(mode="json")


@router.put("/improvements/{improvement_id}")
def decide_improvement(
    project_id: str,
    improvement_id: str,
    payload: dict = Body(...),
    principal: Principal = Depends(current_principal),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    """Approve or reject a recommendation. Approving records the decision; it
    does not silently mutate the approved model or report."""
    authorise_project(s, principal, project_id)
    _, report = _load(s, project_id, "improvements")
    rec = next((r for r in report.recommendations if r.id == improvement_id), None)
    if rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recommendation not found.")
    approve = bool(payload.get("approve"))
    rec.state = "approved" if approve else "rejected"
    rec.decided_by = principal.user_id
    repo.save_artifact(s, project_id, repo.ArtifactKind.IMPROVEMENT, report)
    from ..schemas.agent import AuditAction
    repo.record_audit(
        s, project_id=project_id, actor=principal.user_id,
        action=AuditAction.APPROVAL_DECIDED, target=improvement_id,
        summary=f"Recommendation '{rec.title}' {rec.state}.",
    )
    return {
        "recommendation": rec.model_dump(mode="json"),
        "applied": False,
        "note": "Recorded. Approved recommendations are applied by re-running the "
                "relevant agent, which keeps the change auditable.",
    }
