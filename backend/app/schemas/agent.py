"""Contracts for the orchestrator: tasks, executions, approvals, audit.

The orchestrator keeps a **task dependency graph** per project. A task becomes
runnable when every task it depends on has succeeded and no approval gate it
owns is pending. Agent policy (timeout, retries, budget) lives on the agent
spec, not scattered through call sites.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field

from .common import Contract, Money, Severity, new_id, utcnow


class AgentName(str, Enum):
    ORCHESTRATOR = "orchestrator"
    REQUIREMENTS = "requirements_meeting_agent"
    PROFILING = "data_profiling_agent"
    PREP = "data_preparation_agent"
    SEMANTIC = "semantic_model_agent"
    DAX = "dax_agent"
    REPORT = "report_design_agent"
    QA = "qa_agent"
    IMPROVEMENT = "improvement_agent"


class TaskKind(str, Enum):
    PROFILE_SOURCES = "profile_sources"
    EXTRACT_REQUIREMENTS = "extract_requirements"
    PLAN_TRANSFORMATIONS = "plan_transformations"
    EXECUTE_TRANSFORMATIONS = "execute_transformations"
    PROPOSE_SEMANTIC_MODEL = "propose_semantic_model"
    GENERATE_MEASURES = "generate_measures"
    DESIGN_REPORT = "design_report"
    EXPORT_PBIP = "export_pbip"
    RUN_QA = "run_qa"
    SUGGEST_IMPROVEMENTS = "suggest_improvements"


#: Static dependency template. The orchestrator instantiates this per project
#: and may add QA-triggered rework tasks at runtime.
TASK_DEPENDENCIES: dict[TaskKind, list[TaskKind]] = {
    TaskKind.PROFILE_SOURCES: [],
    TaskKind.EXTRACT_REQUIREMENTS: [TaskKind.PROFILE_SOURCES],
    TaskKind.PLAN_TRANSFORMATIONS: [TaskKind.PROFILE_SOURCES, TaskKind.EXTRACT_REQUIREMENTS],
    TaskKind.EXECUTE_TRANSFORMATIONS: [TaskKind.PLAN_TRANSFORMATIONS],
    TaskKind.PROPOSE_SEMANTIC_MODEL: [TaskKind.EXECUTE_TRANSFORMATIONS],
    TaskKind.GENERATE_MEASURES: [TaskKind.PROPOSE_SEMANTIC_MODEL, TaskKind.EXTRACT_REQUIREMENTS],
    TaskKind.DESIGN_REPORT: [TaskKind.GENERATE_MEASURES],
    TaskKind.RUN_QA: [TaskKind.DESIGN_REPORT],
    TaskKind.EXPORT_PBIP: [TaskKind.RUN_QA],
    TaskKind.SUGGEST_IMPROVEMENTS: [TaskKind.RUN_QA],
}

TASK_OWNER: dict[TaskKind, AgentName] = {
    TaskKind.PROFILE_SOURCES: AgentName.PROFILING,
    TaskKind.EXTRACT_REQUIREMENTS: AgentName.REQUIREMENTS,
    TaskKind.PLAN_TRANSFORMATIONS: AgentName.PREP,
    TaskKind.EXECUTE_TRANSFORMATIONS: AgentName.PREP,
    TaskKind.PROPOSE_SEMANTIC_MODEL: AgentName.SEMANTIC,
    TaskKind.GENERATE_MEASURES: AgentName.DAX,
    TaskKind.DESIGN_REPORT: AgentName.REPORT,
    TaskKind.RUN_QA: AgentName.QA,
    TaskKind.EXPORT_PBIP: AgentName.REPORT,
    TaskKind.SUGGEST_IMPROVEMENTS: AgentName.IMPROVEMENT,
}


class TaskStatus(str, Enum):
    PENDING = "pending"
    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            TaskStatus.SUCCEEDED, TaskStatus.FAILED,
            TaskStatus.SKIPPED, TaskStatus.CANCELLED,
        }


class AgentPolicy(Contract):
    """Per-agent execution limits. Enforced by ``app.agents.base.run_agent``."""

    timeout_seconds: int = 120
    max_retries: int = 1
    retry_backoff_seconds: float = 2.0
    max_usd: float = 0.50
    max_llm_calls: int = 4
    max_output_tokens: int = 4096
    retry_on: list[str] = Field(
        default_factory=lambda: ["timeout", "transient", "schema_invalid"]
    )
    on_exhausted: str = Field(
        default="fail_task",
        description="fail_task | degrade_to_heuristic | skip -- what to do after retries.",
    )


class AgentSpec(Contract):
    """Declared capability surface of one agent, exposed via the API so the UI
    can show what each agent may touch."""

    name: AgentName
    purpose: str
    input_schema: str = Field(description="Fully-qualified Pydantic model name.")
    output_schema: str
    allowed_tools: list[str] = Field(
        default_factory=list,
        description="Service functions this agent may call. Anything else is denied "
        "by ``app.agents.base.ToolBelt``.",
    )
    uses_llm: bool = False
    policy: AgentPolicy = Field(default_factory=AgentPolicy)


class AgentTask(Contract):
    id: str = Field(default_factory=lambda: new_id("task"))
    project_id: str
    kind: TaskKind
    owner: AgentName
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = Field(default_factory=list, description="AgentTask ids.")
    attempt: int = 0
    max_attempts: int = 2
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    input_ref: dict[str, Any] = Field(default_factory=dict)
    result_ref: dict[str, Any] = Field(
        default_factory=dict, description="Ids of the artifacts produced."
    )
    error: str = ""
    requested_by: str = Field(default="orchestrator", description="Or a QA rework request.")
    rework_reason: str = ""


class ExecutionStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BUDGET_EXCEEDED = "budget_exceeded"
    INVALID_OUTPUT = "invalid_output"


class LlmCallRecord(Contract):
    id: str = Field(default_factory=lambda: new_id("llm"))
    provider: str
    model: str
    purpose: str
    prompt_chars: int = 0
    response_chars: int = 0
    cost: Money = Field(default_factory=Money)
    latency_ms: int = 0
    structured_output_valid: bool = True
    error: str = ""
    degraded_to_heuristic: bool = False


class ToolCallRecord(Contract):
    name: str
    arguments_digest: str = ""
    succeeded: bool = True
    duration_ms: int = 0
    error: str = ""


class AgentExecution(Contract):
    id: str = Field(default_factory=lambda: new_id("exec"))
    project_id: str
    task_id: str
    agent: AgentName
    attempt: int = 1
    status: ExecutionStatus = ExecutionStatus.RUNNING
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    duration_ms: int = 0
    input_digest: str = ""
    output_digest: str = ""
    llm_calls: list[LlmCallRecord] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    cost: Money = Field(default_factory=Money)
    log: list[str] = Field(default_factory=list)
    error: str = ""
    error_kind: str = Field(default="", description="timeout | transient | schema_invalid | bug")
    produced: dict[str, Any] = Field(default_factory=dict)


class ApprovalKind(str, Enum):
    REQUIREMENTS = "requirements"
    OPEN_QUESTION = "open_question"
    COLUMN_MAPPING = "column_mapping"
    TRANSFORMATION_STEP = "transformation_step"
    TRANSFORMATION_PLAN = "transformation_plan"
    RELATIONSHIP = "relationship"
    SEMANTIC_MODEL = "semantic_model"
    MEASURE = "measure"
    REPORT = "report"
    IMPROVEMENT = "improvement"
    EXTERNAL_PUBLISH = "external_publish"


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalRequest(Contract):
    id: str = Field(default_factory=lambda: new_id("appr"))
    project_id: str
    kind: ApprovalKind
    subject_id: str = Field(description="Id of the thing being approved.")
    subject_label: str = ""
    question: str = ""
    detail: str = ""
    options: list[str] = Field(default_factory=lambda: ["approve", "reject"])
    severity: Severity = Severity.MEDIUM
    blocks_tasks: list[TaskKind] = Field(default_factory=list)
    state: ApprovalState = ApprovalState.PENDING
    decided_by: str = ""
    decision_note: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None
    requested_by: AgentName = AgentName.ORCHESTRATOR


class AuditAction(str, Enum):
    PROJECT_CREATED = "project_created"
    FILE_UPLOADED = "file_uploaded"
    FILE_REJECTED = "file_rejected"
    FILE_READ = "file_read"
    PROFILE_GENERATED = "profile_generated"
    AGENT_STARTED = "agent_started"
    AGENT_FINISHED = "agent_finished"
    AGENT_FAILED = "agent_failed"
    AGENT_RETRIED = "agent_retried"
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    CODE_GENERATED = "code_generated"
    TRANSFORMATION_EXECUTED = "transformation_executed"
    MODEL_PROPOSED = "model_proposed"
    MEASURE_GENERATED = "measure_generated"
    MEASURE_VALIDATED = "measure_validated"
    REPORT_DESIGNED = "report_designed"
    EXPORT_CREATED = "export_created"
    EXPORT_DOWNLOADED = "export_downloaded"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    QA_RUN = "qa_run"
    REWORK_REQUESTED = "rework_requested"
    AUTH_LOGIN = "auth_login"
    AUTH_DENIED = "auth_denied"
    ERROR = "error"


class AuditEvent(Contract):
    id: str = Field(default_factory=lambda: new_id("audit"))
    project_id: str = ""
    at: datetime = Field(default_factory=utcnow)
    actor: str = Field(description="User id, agent name, or 'system'.")
    action: AuditAction
    summary: str = ""
    target: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    severity: Severity = Severity.INFO
    execution_id: str = ""


class TaskGraph(Contract):
    project_id: str
    tasks: list[AgentTask] = Field(default_factory=list)

    def by_id(self, task_id: str) -> AgentTask | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def by_kind(self, kind: TaskKind) -> AgentTask | None:
        return next((t for t in self.tasks if t.kind is kind), None)

    def runnable(self) -> list[AgentTask]:
        done = {t.id for t in self.tasks if t.status is TaskStatus.SUCCEEDED}
        out = []
        for t in self.tasks:
            if t.status in {TaskStatus.PENDING, TaskStatus.BLOCKED, TaskStatus.READY}:
                if all(d in done for d in t.depends_on):
                    out.append(t)
        return out

    @property
    def progress(self) -> float:
        if not self.tasks:
            return 0.0
        done = sum(1 for t in self.tasks if t.status is TaskStatus.SUCCEEDED)
        return round(done / len(self.tasks), 4)
