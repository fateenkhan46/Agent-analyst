"""The orchestrator.

Responsibilities, in the brief's terms: maintain the dependency graph, assign
work, collect results, manage retries, and decide when user approval is
required.

The key design point is ``_payload_for`` / ``_absorb``: the orchestrator is the
only component that knows how one agent's output becomes the next agent's
input. Agents never read each other's internals; they exchange the typed
artifacts the orchestrator persists and hands on. That is what makes this a
coordinated workflow rather than nine chatbots.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..schemas.agent import (
    AgentName, AgentTask, ApprovalKind, ApprovalRequest, ApprovalState, AuditAction,
    ExecutionStatus, TaskGraph, TaskKind, TaskStatus,
)
from ..schemas.common import Contract, Severity
from ..schemas.project import ProjectStatus
from ..services import repository as repo
from ..agents.registry import get_agent
from .graph import ensure_task_graph, refresh_statuses

#: Tasks that must not start while an approval they depend on is unresolved.
APPROVAL_GATES: dict[TaskKind, tuple[ApprovalKind, ...]] = {
    TaskKind.EXECUTE_TRANSFORMATIONS: (ApprovalKind.TRANSFORMATION_PLAN,
                                       ApprovalKind.TRANSFORMATION_STEP),
    TaskKind.GENERATE_MEASURES: (ApprovalKind.SEMANTIC_MODEL,),
}


class OrchestrationBlocked(Exception):
    """Raised when a task cannot proceed for a reason the user must resolve."""

    def __init__(self, reason: str, *, approvals: list[ApprovalRequest] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.approvals = approvals or []


def _pending_gate_approvals(s: Session, project_id: str, kind: TaskKind) -> list[ApprovalRequest]:
    gates = APPROVAL_GATES.get(kind, ())
    if not gates:
        return []
    pending = repo.list_approvals(s, project_id, state=ApprovalState.PENDING)
    return [a for a in pending if a.kind in gates]


def run_task(
    s: Session, project_id: str, kind: TaskKind, *, actor: str = "orchestrator",
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one task: check dependencies and gates, dispatch, absorb the result."""
    graph = refresh_statuses(s, project_id)
    task = graph.by_kind(kind)
    if task is None:
        graph = ensure_task_graph(s, project_id)
        task = graph.by_kind(kind)
    assert task is not None

    done = {t.id for t in graph.tasks if t.status is TaskStatus.SUCCEEDED}
    missing = [d for d in task.depends_on if d not in done]
    if missing:
        names = sorted(
            {t.kind.value for t in graph.tasks if t.id in missing}
        )
        raise OrchestrationBlocked(
            f"'{kind.value}' cannot run yet: {', '.join(names)} must succeed first."
        )

    gate = _pending_gate_approvals(s, project_id, kind)
    if gate:
        task.status = TaskStatus.AWAITING_APPROVAL
        repo.save_task(s, task)
        raise OrchestrationBlocked(
            f"'{kind.value}' is waiting on {len(gate)} approval(s).", approvals=gate,
        )

    agent = get_agent(task.owner)
    payload = _payload_for(s, project_id, kind, task, extra_payload or {})

    task.status = TaskStatus.RUNNING
    task.attempt += 1
    repo.save_task(s, task)
    repo.set_project_status(s, project_id, _status_for(kind))

    execution, result = agent.execute_with_retries(
        s, task_id=task.id, project_id=project_id, actor=actor, payload=payload
    )

    if execution.status is not ExecutionStatus.SUCCEEDED or result is None:
        task.status = TaskStatus.FAILED
        task.error = execution.error
        repo.save_task(s, task)
        repo.set_project_status(s, project_id, ProjectStatus.FAILED)
        return {
            "task": task.model_dump(mode="json"),
            "execution": execution.model_dump(mode="json"),
            "succeeded": False,
            "error": execution.error,
        }

    produced = _absorb(s, project_id, kind, result)
    task.status = TaskStatus.SUCCEEDED
    task.result_ref = produced
    task.error = ""
    repo.save_task(s, task)
    refresh_statuses(s, project_id)
    _recompute_project_status(s, project_id)

    return {
        "task": task.model_dump(mode="json"),
        "execution": execution.model_dump(mode="json"),
        "succeeded": True,
        "produced": produced,
        "result": result.model_dump(mode="json"),
    }


def _status_for(kind: TaskKind) -> ProjectStatus:
    return {
        TaskKind.PROFILE_SOURCES: ProjectStatus.PROFILING,
        TaskKind.RUN_QA: ProjectStatus.QA,
    }.get(kind, ProjectStatus.BUILDING)


def _recompute_project_status(s: Session, project_id: str) -> None:
    graph = repo.load_task_graph(s, project_id)
    pending_approvals = [
        a for a in repo.list_approvals(s, project_id, state=ApprovalState.PENDING)
    ]
    if pending_approvals:
        repo.set_project_status(s, project_id, ProjectStatus.AWAITING_APPROVAL)
        return
    terminal_kinds = {TaskKind.EXPORT_PBIP, TaskKind.SUGGEST_IMPROVEMENTS}
    if all(
        t.status is TaskStatus.SUCCEEDED
        for t in graph.tasks if t.kind in terminal_kinds
    ) and graph.by_kind(TaskKind.EXPORT_PBIP):
        repo.set_project_status(s, project_id, ProjectStatus.READY)
    else:
        repo.set_project_status(s, project_id, ProjectStatus.BUILDING)


# --------------------------------------------------------------------------
# Payload assembly: the only place that knows the inter-agent wiring.
# --------------------------------------------------------------------------
def _payload_for(
    s: Session, project_id: str, kind: TaskKind, task: AgentTask, extra: dict[str, Any]
) -> dict[str, Any]:
    project = repo.get_project(s, project_id)
    assert project is not None
    base: dict[str, Any] = {"project_id": project_id}

    if kind is TaskKind.PROFILE_SOURCES:
        base["reprofile"] = bool(extra.get("reprofile"))

    elif kind is TaskKind.EXTRACT_REQUIREMENTS:
        from ..schemas.source import SourceDataProfile
        from ..services.storage import get_store
        profile = repo.load_artifact(s, project_id, repo.ArtifactKind.PROFILE, SourceDataProfile)
        transcript = ""
        transcript_id = ""
        for f in repo.list_source_files(s, project_id):
            if f.kind.value == "transcript" and f.stored_path:
                try:
                    transcript = get_store().get_bytes(f.stored_path).decode("utf-8", "replace")
                    transcript_id = f.id
                    break
                except FileNotFoundError:
                    continue
        base.update({
            "project_name": project.name,
            "description": project.description,
            "business_objective": project.business_objective,
            "stated_kpis": project.desired_kpis,
            "reporting_questions": project.reporting_questions,
            "reporting_period": project.reporting_period,
            "currency": project.currency,
            "units": project.units,
            "business_definitions": project.business_definitions,
            "transcript_text": transcript,
            "transcript_source_file_id": transcript_id,
            "design_preferences": project.design_preferences.notes,
            "available_tables": [t.table_name for t in (profile.tables if profile else [])],
            "available_columns": {
                t.table_name: [c.name for c in t.columns]
                for t in (profile.tables if profile else [])
            },
        })

    elif kind is TaskKind.PLAN_TRANSFORMATIONS:
        base.update({
            "profile_id": project.profile_id,
            "requirements_id": project.requirements_id,
            "tables": extra.get("tables", []),
            "target_kpis": extra.get("target_kpis", []),
            "user_mapping_overrides": extra.get("user_mapping_overrides", {}),
        })

    elif kind is TaskKind.EXECUTE_TRANSFORMATIONS:
        base.update({"plan_id": project.plan_id, "profile_id": project.profile_id})

    elif kind is TaskKind.PROPOSE_SEMANTIC_MODEL:
        base.update({
            "datasets": [d["name"] for d in repo.list_datasets(s, project_id)],
            "requirements_id": project.requirements_id,
            "profile_id": project.profile_id,
            "join_proposal_ids": extra.get("join_proposal_ids", []),
        })

    elif kind is TaskKind.GENERATE_MEASURES:
        base.update({
            "model_id": project.model_id,
            "requirements_id": project.requirements_id,
            "kpi_ids": extra.get("kpi_ids", []),
            "qa_feedback": extra.get("qa_feedback", []) or _qa_feedback_for(s, project_id, AgentName.DAX),
        })

    elif kind is TaskKind.DESIGN_REPORT:
        base.update({
            "model_id": project.model_id,
            "requirements_id": project.requirements_id,
            "design_preferences": project.design_preferences.notes,
            "max_pages": project.design_preferences.page_count_hint or 5,
            "qa_feedback": extra.get("qa_feedback", []) or _qa_feedback_for(s, project_id, AgentName.REPORT),
        })

    elif kind is TaskKind.RUN_QA:
        base.update({
            "model_id": project.model_id, "report_id": project.report_id,
            "plan_id": project.plan_id, "execution_id": project.execution_id,
            "requirements_id": project.requirements_id,
            "categories": extra.get("categories", []),
        })

    elif kind is TaskKind.EXPORT_PBIP:
        base.update({"model_id": project.model_id, "report_id": project.report_id})

    elif kind is TaskKind.SUGGEST_IMPROVEMENTS:
        base.update({
            "model_id": project.model_id, "report_id": project.report_id,
            "plan_id": project.plan_id, "requirements_id": project.requirements_id,
            "validation_report_id": project.validation_report_id,
        })

    base.update({k: v for k, v in extra.items() if k not in base})
    return base


def _qa_feedback_for(s: Session, project_id: str, agent: AgentName) -> list[str]:
    """Collect the instructions QA left for a specific agent, so a rework run
    actually receives the failure it is meant to fix."""
    from ..schemas.qa import ValidationReport
    report = repo.load_artifact(s, project_id, repo.ArtifactKind.VALIDATION, ValidationReport)
    if report is None:
        return []
    out: list[str] = []
    for rr in report.rework_requests:
        if rr.target_agent == agent.value:
            out.append(rr.reason)
            out.extend(rr.instructions)
    return out


def _absorb(s: Session, project_id: str, kind: TaskKind, result: Contract) -> dict[str, Any]:
    """Persist an agent's output and point the project at the new artifacts."""
    project = repo.get_project(s, project_id)
    assert project is not None
    produced: dict[str, Any] = {}

    if kind is TaskKind.PROFILE_SOURCES:
        produced["profile_id"] = getattr(result, "profile").id
        project.profile_id = produced["profile_id"]

    elif kind is TaskKind.EXTRACT_REQUIREMENTS:
        spec = getattr(result, "spec")
        repo.save_artifact(s, project_id, repo.ArtifactKind.REQUIREMENTS, spec)
        project.requirements_id = spec.id
        produced["requirements_id"] = spec.id
        minutes = getattr(result, "minutes", None)
        if minutes is not None:
            repo.save_artifact(s, project_id, repo.ArtifactKind.MINUTES, minutes)
            project.minutes_id = minutes.id
            produced["minutes_id"] = minutes.id
        _raise_question_approvals(s, project_id, spec)

    elif kind is TaskKind.PLAN_TRANSFORMATIONS:
        plan = getattr(result, "plan")
        repo.save_artifact(s, project_id, repo.ArtifactKind.PLAN, plan)
        project.plan_id = plan.id
        produced["plan_id"] = plan.id
        _raise_plan_approvals(s, project_id, plan)

    elif kind is TaskKind.EXECUTE_TRANSFORMATIONS:
        ex = getattr(result, "execution")
        repo.save_artifact(s, project_id, repo.ArtifactKind.EXECUTION, ex)
        project.execution_id = ex.id
        produced["execution_id"] = ex.id
        produced["datasets"] = [d.name for d in ex.datasets]

    elif kind is TaskKind.PROPOSE_SEMANTIC_MODEL:
        model = getattr(result, "model")
        repo.save_artifact(s, project_id, repo.ArtifactKind.MODEL, model)
        project.model_id = model.id
        produced["model_id"] = model.id
        _raise_model_approvals(s, project_id, model)

    elif kind is TaskKind.GENERATE_MEASURES:
        from ..schemas.semantic import SemanticModel
        model = repo.load_artifact(s, project_id, repo.ArtifactKind.MODEL, SemanticModel)
        measures = getattr(result, "measures")
        if model is not None:
            by_name = {m.name.lower(): m for m in model.measures}
            for m in measures:
                by_name[m.name.lower()] = m
            model.measures = list(by_name.values())
            model.version += 1
            repo.save_artifact(s, project_id, repo.ArtifactKind.MODEL, model)
            project.model_id = model.id
            produced["model_id"] = model.id
        produced["measure_count"] = len(measures)
        produced["rejected_count"] = len(getattr(result, "rejected", []))

    elif kind is TaskKind.DESIGN_REPORT:
        report = getattr(result, "report")
        repo.save_artifact(s, project_id, repo.ArtifactKind.REPORT, report)
        project.report_id = report.id
        produced["report_id"] = report.id

    elif kind is TaskKind.RUN_QA:
        vr = getattr(result, "report")
        repo.save_artifact(s, project_id, repo.ArtifactKind.VALIDATION, vr)
        project.validation_report_id = vr.id
        produced["validation_report_id"] = vr.id
        produced["passed"] = vr.passed
        produced["counts"] = vr.counts
        _queue_rework(s, project_id, vr)

    elif kind is TaskKind.EXPORT_PBIP:
        arts = getattr(result, "artifacts", [])
        produced["exports"] = [a.id for a in arts]

    elif kind is TaskKind.SUGGEST_IMPROVEMENTS:
        ir = getattr(result, "report")
        repo.save_artifact(s, project_id, repo.ArtifactKind.IMPROVEMENT, ir)
        project.improvement_report_id = ir.id
        produced["improvement_report_id"] = ir.id

    repo.save_project(s, project)
    return produced


# --------------------------------------------------------------------------
# Approval gates
# --------------------------------------------------------------------------
def _request_approval(
    s: Session, project_id: str, *, kind: ApprovalKind, subject_id: str, label: str,
    question: str, detail: str = "", severity: Severity = Severity.MEDIUM,
    blocks: list[TaskKind] | None = None, requested_by: AgentName = AgentName.ORCHESTRATOR,
) -> ApprovalRequest | None:
    if repo.find_approval(s, project_id, subject_id):
        return None
    req = ApprovalRequest(
        project_id=project_id, kind=kind, subject_id=subject_id, subject_label=label,
        question=question, detail=detail, severity=severity,
        blocks_tasks=blocks or [], requested_by=requested_by,
    )
    repo.save_approval(s, req)
    repo.record_audit(
        s, project_id=project_id, actor=requested_by.value,
        action=AuditAction.APPROVAL_REQUESTED, target=subject_id,
        summary=f"Approval requested: {question[:180]}", severity=severity,
    )
    return req


def _raise_question_approvals(s: Session, project_id: str, spec: Contract) -> None:
    for q in getattr(spec, "open_questions", []):
        if q.answered:
            continue
        _request_approval(
            s, project_id, kind=ApprovalKind.OPEN_QUESTION, subject_id=q.id,
            label=q.question, question=q.question, detail=q.why_it_matters,
            severity=q.severity, requested_by=AgentName.REQUIREMENTS,
        )
    for a in getattr(spec, "assumptions", []):
        if a.confirmed is not None:
            continue
        _request_approval(
            s, project_id, kind=ApprovalKind.REQUIREMENTS, subject_id=a.id,
            label=a.statement, question=f"Is this assumption correct? {a.statement}",
            detail=a.impact_if_wrong, severity=a.severity,
            requested_by=AgentName.REQUIREMENTS,
        )


def _raise_plan_approvals(s: Session, project_id: str, plan: Contract) -> None:
    for step in getattr(plan, "steps", []):
        if not step.requires_approval or step.approved is not None:
            continue
        _request_approval(
            s, project_id, kind=ApprovalKind.TRANSFORMATION_STEP, subject_id=step.id,
            label=step.title or step.op.value,
            question=f"Approve this {step.risk.value} transformation on "
                     f"{step.target_table}: {step.title or step.op.value}?",
            detail=f"{step.reason}\n\nExpected effect: {step.expected_effect}",
            severity=Severity.HIGH if step.risk.value == "destructive" else Severity.MEDIUM,
            blocks=[TaskKind.EXECUTE_TRANSFORMATIONS], requested_by=AgentName.PREP,
        )
    for m in getattr(plan, "mappings", []):
        if m.accepted is not None or m.confidence.value != "low":
            continue
        _request_approval(
            s, project_id, kind=ApprovalKind.COLUMN_MAPPING, subject_id=m.id,
            label=f"{m.source_table}.{m.source_column} -> {m.canonical_name}",
            question=f"Map '{m.source_table}.{m.source_column}' to "
                     f"'{m.canonical_name}'?",
            detail=m.reason, requested_by=AgentName.PREP,
        )


def _raise_model_approvals(s: Session, project_id: str, model: Contract) -> None:
    for r in getattr(model, "relationships", []):
        if not r.requires_approval or r.approved is not None:
            continue
        _request_approval(
            s, project_id, kind=ApprovalKind.RELATIONSHIP, subject_id=r.id,
            label=r.key,
            question=f"Create the relationship {r.key} ({r.cardinality.value})?",
            detail=f"{r.rationale}\n\nEvidence: " + "; ".join(r.evidence[:4]),
            severity=Severity.HIGH if r.cardinality.value == "many_to_many" else Severity.MEDIUM,
            blocks=[TaskKind.GENERATE_MEASURES], requested_by=AgentName.SEMANTIC,
        )


def _queue_rework(s: Session, project_id: str, validation_report: Contract) -> None:
    """Turn QA's rework requests into real tasks the orchestrator will run."""
    from .graph import add_rework_task

    kind_by_agent = {
        AgentName.PREP.value: TaskKind.PLAN_TRANSFORMATIONS,
        AgentName.SEMANTIC.value: TaskKind.PROPOSE_SEMANTIC_MODEL,
        AgentName.DAX.value: TaskKind.GENERATE_MEASURES,
        AgentName.REPORT.value: TaskKind.DESIGN_REPORT,
        AgentName.REQUIREMENTS.value: TaskKind.EXTRACT_REQUIREMENTS,
    }
    for rr in getattr(validation_report, "rework_requests", []):
        kind = kind_by_agent.get(rr.target_agent)
        if kind is None:
            continue
        add_rework_task(s, project_id, kind, reason=rr.reason)
        repo.record_audit(
            s, project_id=project_id, actor=AgentName.QA.value,
            action=AuditAction.REWORK_REQUESTED, severity=rr.severity,
            summary=f"QA returned work to {rr.target_agent}: {rr.reason[:200]}",
        )


def decide_approval(
    s: Session, project_id: str, approval_id: str, *, approve: bool,
    decided_by: str, note: str = "",
) -> ApprovalRequest:
    """Record a decision and write it through to the underlying artifact."""
    import datetime as _dt

    req = repo.get_approval(s, approval_id)
    if req is None or req.project_id != project_id:
        raise KeyError(approval_id)
    req.state = ApprovalState.APPROVED if approve else ApprovalState.REJECTED
    req.decided_by = decided_by
    req.decision_note = note
    req.decided_at = _dt.datetime.now(_dt.timezone.utc)
    repo.save_approval(s, req)
    _apply_decision(s, project_id, req, approve=approve, note=note)
    repo.record_audit(
        s, project_id=project_id, actor=decided_by, action=AuditAction.APPROVAL_DECIDED,
        target=req.subject_id,
        summary=f"{'Approved' if approve else 'Rejected'}: {req.subject_label[:160]}",
        detail={"kind": req.kind.value, "note": note},
    )
    refresh_statuses(s, project_id)
    _recompute_project_status(s, project_id)
    return req


def _apply_decision(
    s: Session, project_id: str, req: ApprovalRequest, *, approve: bool, note: str
) -> None:
    from ..schemas.prep import TransformationPlan
    from ..schemas.requirements import RequirementsSpec
    from ..schemas.semantic import SemanticModel

    if req.kind in {ApprovalKind.TRANSFORMATION_STEP, ApprovalKind.COLUMN_MAPPING,
                    ApprovalKind.TRANSFORMATION_PLAN}:
        plan = repo.load_artifact(s, project_id, repo.ArtifactKind.PLAN, TransformationPlan)
        if plan is None:
            return
        for step in plan.steps:
            if step.id == req.subject_id:
                step.approved = approve
        for m in plan.mappings:
            if m.id == req.subject_id:
                m.accepted = approve
        if req.kind is ApprovalKind.TRANSFORMATION_PLAN and approve:
            plan.approved = True
        repo.save_artifact(s, project_id, repo.ArtifactKind.PLAN, plan)

    elif req.kind in {ApprovalKind.RELATIONSHIP, ApprovalKind.SEMANTIC_MODEL,
                      ApprovalKind.MEASURE}:
        model = repo.load_artifact(s, project_id, repo.ArtifactKind.MODEL, SemanticModel)
        if model is None:
            return
        for r in model.relationships:
            if r.id == req.subject_id:
                r.approved = approve
                r.is_active = r.is_active and approve
        for m in model.measures:
            if m.id == req.subject_id:
                m.approved = approve
        if req.kind is ApprovalKind.SEMANTIC_MODEL and approve:
            model.approved = True
        repo.save_artifact(s, project_id, repo.ArtifactKind.MODEL, model)

    elif req.kind in {ApprovalKind.OPEN_QUESTION, ApprovalKind.REQUIREMENTS}:
        spec = repo.load_artifact(s, project_id, repo.ArtifactKind.REQUIREMENTS, RequirementsSpec)
        if spec is None:
            return
        import datetime as _dt
        for q in spec.open_questions:
            if q.id == req.subject_id:
                q.answer = note or ("Confirmed." if approve else "Rejected.")
                q.answered_at = _dt.datetime.now(_dt.timezone.utc)
        for a in spec.assumptions:
            if a.id == req.subject_id:
                a.confirmed = approve
        if req.kind is ApprovalKind.REQUIREMENTS and approve and not spec.blocking_questions:
            spec.approved = True
        repo.save_artifact(s, project_id, repo.ArtifactKind.REQUIREMENTS, spec)


def run_pipeline(
    s: Session, project_id: str, *, actor: str = "orchestrator",
    stop_after: TaskKind | None = None, auto_approve: bool = False,
) -> list[dict[str, Any]]:
    """Drive the graph until it blocks, fails, or finishes.

    ``auto_approve`` is for the sample project and tests only: it answers
    pending gates automatically. It is never the default, because the whole
    point of the gates is that a human sees ambiguous or destructive changes.
    """
    ensure_task_graph(s, project_id)
    results: list[dict[str, Any]] = []
    order = [
        TaskKind.PROFILE_SOURCES, TaskKind.EXTRACT_REQUIREMENTS,
        TaskKind.PLAN_TRANSFORMATIONS, TaskKind.EXECUTE_TRANSFORMATIONS,
        TaskKind.PROPOSE_SEMANTIC_MODEL, TaskKind.GENERATE_MEASURES,
        TaskKind.DESIGN_REPORT, TaskKind.RUN_QA, TaskKind.EXPORT_PBIP,
        TaskKind.SUGGEST_IMPROVEMENTS,
    ]
    for kind in order:
        graph = refresh_statuses(s, project_id)
        task = graph.by_kind(kind)
        if task is not None and task.status is TaskStatus.SUCCEEDED:
            continue
        if auto_approve:
            for a in repo.list_approvals(s, project_id, state=ApprovalState.PENDING):
                decide_approval(
                    s, project_id, a.id, approve=True, decided_by=f"{actor}:auto_approve",
                    note="Auto-approved by run_pipeline(auto_approve=True).",
                )
        try:
            outcome = run_task(s, project_id, kind, actor=actor)
        except OrchestrationBlocked as exc:
            results.append({
                "kind": kind.value, "succeeded": False, "blocked": True,
                "reason": exc.reason,
                "approvals": [a.model_dump(mode="json") for a in exc.approvals],
            })
            break
        except KeyError as exc:
            results.append({
                "kind": kind.value, "succeeded": False, "blocked": True,
                "reason": f"No agent available: {exc}",
            })
            break
        outcome["kind"] = kind.value
        results.append(outcome)
        if not outcome.get("succeeded"):
            break
        if stop_after is not None and kind is stop_after:
            break
    return results
