"""Assembling the delivery bundle.

Rule: a section is ``available`` only when the artifact exists, and an export
is listed only when the file is present on disk right now. Everything else
carries the reason it is missing. This is the function that keeps the delivery
page from claiming work that was not done.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..schemas.prep import TransformationExecution, TransformationPlan
from ..schemas.project import DeliveryBundle
from ..schemas.qa import ImprovementReport, ValidationReport
from ..schemas.report import ReportSpec
from ..schemas.requirements import MeetingMinutes, RequirementsSpec
from ..schemas.semantic import SemanticModel
from ..schemas.source import SourceDataProfile
from . import repository as repo
from .capabilities import capabilities
from .storage import get_store

MISSING_REASONS = {
    "source_profile": "No source has been profiled yet. Upload data and run the profiling agent.",
    "minutes": "No meeting transcript was supplied, so there are no minutes to produce.",
    "requirements": "The requirements agent has not run yet.",
    "transformation_plan": "The data preparation agent has not produced a plan yet.",
    "transformation_lineage": "No transformation has been executed yet.",
    "semantic_model": "The semantic model agent has not run yet.",
    "measures": "No DAX measures have been generated yet.",
    "report": "The report design agent has not run yet.",
    "validation": "QA has not run yet.",
    "improvements": "The improvement agent has not run yet.",
}


def build_delivery_bundle(s: Session, project_id: str) -> DeliveryBundle:
    project = repo.get_project(s, project_id)
    if project is None:
        raise KeyError(project_id)

    profile = repo.load_artifact(s, project_id, repo.ArtifactKind.PROFILE, SourceDataProfile)
    minutes = repo.load_artifact(s, project_id, repo.ArtifactKind.MINUTES, MeetingMinutes)
    reqs = repo.load_artifact(s, project_id, repo.ArtifactKind.REQUIREMENTS, RequirementsSpec)
    plan = repo.load_artifact(s, project_id, repo.ArtifactKind.PLAN, TransformationPlan)
    execution = repo.load_artifact(
        s, project_id, repo.ArtifactKind.EXECUTION, TransformationExecution
    )
    model = repo.load_artifact(s, project_id, repo.ArtifactKind.MODEL, SemanticModel)
    report = repo.load_artifact(s, project_id, repo.ArtifactKind.REPORT, ReportSpec)
    validation = repo.load_artifact(s, project_id, repo.ArtifactKind.VALIDATION, ValidationReport)
    improvements = repo.load_artifact(
        s, project_id, repo.ArtifactKind.IMPROVEMENT, ImprovementReport
    )

    def section(
        key: str, obj: Any, href: str, summary: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if obj is None:
            return {"available": False, "reason": MISSING_REASONS.get(key, "Not produced yet."),
                    "href": href, "summary": {}}
        return {"available": True, "reason": "", "href": href, "summary": summary or {}}

    base = f"/api/v1/projects/{project_id}"
    sections: dict[str, dict] = {
        "source_profile": section(
            "source_profile", profile, f"{base}/profile",
            profile and {
                "tables": len(profile.tables),
                "total_rows": profile.total_rows,
                "issues": profile.issue_counts(),
                "key_candidates": len(profile.key_overlaps),
            },
        ),
        "minutes_of_meeting": section(
            "minutes", minutes, f"{base}/minutes.md",
            minutes and {
                "attendees": len(minutes.attendees),
                "decisions": len(minutes.decisions),
                "action_items": len(minutes.action_items),
                "unassigned_action_items": len(minutes.unassigned_action_items),
                "open_questions": len(minutes.open_questions),
                "produced_by": minutes.provenance.origin.value,
            },
        ),
        "requirements": section(
            "requirements", reqs, f"{base}/artifacts/requirements",
            reqs and {
                "kpis": len(reqs.kpis),
                "kpis_needing_confirmation": sum(1 for k in reqs.kpis if k.needs_confirmation),
                "dimensions": len(reqs.dimensions),
                "approved": reqs.approved,
                "produced_by": reqs.provenance.origin.value,
            },
        ),
        "transformation_plan": section(
            "transformation_plan", plan, f"{base}/artifacts/plan",
            plan and {
                "steps": len(plan.steps),
                "mappings": len(plan.mappings),
                "awaiting_approval": len(plan.steps_awaiting_approval),
                "join_proposals": len(plan.join_proposals),
                "approved": plan.approved,
            },
        ),
        "transformation_lineage": section(
            "transformation_lineage", execution, f"{base}/artifacts/execution",
            execution and {
                "succeeded": execution.succeeded,
                "datasets": [d.name for d in execution.datasets],
                "skipped_steps": len(execution.skipped_steps),
                "lineage_edges": len(execution.lineage),
                "quality_before": execution.quality_before.score if execution.quality_before else None,
                "quality_after": execution.quality_after.score if execution.quality_after else None,
                "m_queries": sorted(execution.m_queries),
            },
        ),
        "semantic_model": section(
            "semantic_model", model, f"{base}/artifacts/model",
            model and {
                "tables": len(model.tables),
                "relationships": len(model.relationships),
                "shape": model.schema_shape,
                "relationships_awaiting_approval": len(model.relationships_awaiting_approval),
                "approved": model.approved,
                "has_date_dimension": model.has_date_dimension,
            },
        ),
        "measure_catalog": section(
            "measures", model if (model and model.measures) else None,
            f"{base}/artifacts/model",
            model and {
                "measures": len(model.measures),
                "with_validation_cases": sum(1 for m in model.measures if m.validation_cases),
                "names": [m.name for m in model.measures],
            },
        ),
        "report_catalog": section(
            "report", report, f"{base}/artifacts/report",
            report and {
                "pages": len(report.pages),
                "visuals": report.visual_count,
                "unmapped_kpis": report.unmapped_kpis,
            },
        ),
        "validation": section(
            "validation", validation, f"{base}/artifacts/validation",
            validation and {
                "counts": validation.counts,
                "passed": validation.passed,
                "blocking_failures": len(validation.blocking_failures),
                "engines_used": validation.engines_used(),
                "scope_note": validation.validation_scope_note,
            },
        ),
        "improvements": section(
            "improvements", improvements, f"{base}/artifacts/improvements",
            improvements and {
                "recommendations": len(improvements.recommendations),
                "by_area": improvements.by_area(),
            },
        ),
        "execution_log": {
            "available": True, "reason": "", "href": f"{base}/executions",
            "summary": {"executions": len(repo.list_executions(s, project_id=project_id, limit=500))},
        },
        "audit_log": {
            "available": True, "reason": "", "href": f"{base}/audit",
            "summary": {"events": repo.count_audit(s, project_id)},
        },
    }

    store = get_store()
    exports: list[dict[str, Any]] = []
    for art in repo.list_exports(s, project_id):
        present = store.exists(art.stored_path)
        exports.append({
            **art.model_dump(mode="json"),
            "file_present": present,
            "download_href": f"{base}/exports/{art.id}/download" if present else "",
            "note": "" if present
            else "The file is recorded but missing from storage; re-run the export.",
        })

    questions = []
    if reqs:
        questions = [
            {**q.model_dump(mode="json"), "source": "requirements"}
            for q in reqs.open_questions if not q.answered
        ]
    if minutes:
        known = {q["id"] for q in questions}
        questions += [
            {**q.model_dump(mode="json"), "source": "minutes"}
            for q in minutes.open_questions if not q.answered and q.id not in known
        ]

    assumptions = []
    if reqs:
        assumptions = [
            a.model_dump(mode="json") for a in reqs.assumptions if a.confirmed is None
        ]

    return DeliveryBundle(
        project=project,
        capabilities=capabilities(),
        sections=sections,
        exports=exports,
        unresolved_questions=questions,
        unconfirmed_assumptions=assumptions,
    )
