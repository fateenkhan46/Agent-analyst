"""The Project aggregate and the delivery view."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from .common import Capability, Contract, Money, new_id, utcnow


class ProjectStatus(str, Enum):
    DRAFT = "draft"
    PROFILING = "profiling"
    AWAITING_APPROVAL = "awaiting_approval"
    BUILDING = "building"
    QA = "qa"
    READY = "ready"
    FAILED = "failed"


class DesignPreferences(Contract):
    theme_hint: str = ""
    brand_colors: list[str] = Field(default_factory=list)
    page_count_hint: int = 0
    audience: str = ""
    notes: str = ""


class ProjectCreate(Contract):
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    business_objective: str = ""
    desired_kpis: list[str] = Field(default_factory=list)
    reporting_questions: list[str] = Field(default_factory=list)
    reporting_period: str = ""
    currency: str = ""
    units: str = ""
    business_definitions: dict[str, str] = Field(default_factory=dict)
    meeting_transcript: str = Field(
        default="",
        description="Pasted transcript. A transcript may also be uploaded as a file; "
        "both feed the MoM.",
    )
    design_preferences: DesignPreferences = Field(default_factory=DesignPreferences)


class Project(Contract):
    id: str = Field(default_factory=lambda: new_id("proj"))
    owner_id: str
    tenant_id: str = "default"
    name: str
    description: str = ""
    business_objective: str = ""
    desired_kpis: list[str] = Field(default_factory=list)
    reporting_questions: list[str] = Field(default_factory=list)
    reporting_period: str = ""
    currency: str = ""
    units: str = ""
    business_definitions: dict[str, str] = Field(default_factory=dict)
    design_preferences: DesignPreferences = Field(default_factory=DesignPreferences)
    status: ProjectStatus = ProjectStatus.DRAFT
    workspace_path: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    spend: Money = Field(default_factory=Money)
    budget_usd: float = Field(default=5.0, description="Hard cap on LLM spend per project.")

    # ids of the current artifact versions
    profile_id: str = ""
    minutes_id: str = ""
    requirements_id: str = ""
    plan_id: str = ""
    execution_id: str = ""
    model_id: str = ""
    report_id: str = ""
    validation_report_id: str = ""
    improvement_report_id: str = ""


class DeliveryBundle(Contract):
    """What the delivery page renders. Every section is either present with
    real content or explicitly marked unavailable with a reason."""

    project: Project
    capabilities: list[Capability] = Field(default_factory=list)
    sections: dict[str, dict] = Field(
        default_factory=dict,
        description="section key -> {available: bool, reason: str, href: str, summary: dict}",
    )
    exports: list[dict] = Field(default_factory=list)
    unresolved_questions: list[dict] = Field(default_factory=list)
    unconfirmed_assumptions: list[dict] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utcnow)
