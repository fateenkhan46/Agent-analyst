"""Contracts for Step 3: interpreting the brief and the meeting transcript.

Two distinct deliverables come out of this step and they must not be
conflated:

* ``MeetingMinutes`` -- the **MoM**: a faithful record of what was said in the
  stakeholder meeting (attendees, agenda, discussion, decisions, action items,
  open questions). It is a *minute*, so every statement must be traceable to a
  transcript span; nothing may be invented.
* ``RequirementsSpec`` -- the engineering specification the downstream agents
  actually build from, synthesised from the brief **and** the MoM.

Keeping them separate is what lets the MoM be circulated to stakeholders for
sign-off while the spec drives the pipeline.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from .common import Confidence, Contract, Origin, Provenance, Severity, new_id, utcnow


# --------------------------------------------------------------------------
# Minutes of Meeting
# --------------------------------------------------------------------------
class TranscriptSpan(Contract):
    """Pointer back into the transcript so every MoM line is auditable."""

    start_line: int
    end_line: int
    speaker: str = ""
    quote: str = Field(default="", description="Verbatim excerpt, truncated to 400 chars.")


class Attendee(Contract):
    name: str
    role: str = ""
    organisation: str = ""
    speaking_turns: int = 0
    inferred: bool = Field(
        default=False,
        description="True when the name was derived from speaker labels rather than "
        "stated explicitly; flagged in the MoM so it can be corrected.",
    )


class DiscussionPoint(Contract):
    topic: str
    summary: str
    participants: list[str] = Field(default_factory=list)
    spans: list[TranscriptSpan] = Field(default_factory=list)


class Decision(Contract):
    """A settled choice. Only recorded when the transcript contains explicit
    agreement language -- otherwise it belongs in ``open_questions``."""

    id: str = Field(default_factory=lambda: new_id("dec"))
    statement: str
    decided_by: str = ""
    affects: list[str] = Field(
        default_factory=list, description="e.g. 'kpi:Budget Variance', 'model:FactCost'"
    )
    spans: list[TranscriptSpan] = Field(default_factory=list)
    confidence: Confidence = Confidence.MEDIUM


class ActionItem(Contract):
    id: str = Field(default_factory=lambda: new_id("act"))
    description: str
    owner: str = Field(default="", description="Empty means unassigned -- surfaced as a gap.")
    due: str = Field(default="", description="As stated in the meeting; not normalised.")
    status: str = "open"
    spans: list[TranscriptSpan] = Field(default_factory=list)


class OpenQuestion(Contract):
    """Something the meeting did not settle. These become the targeted
    questions we put to the user instead of inventing a definition."""

    id: str = Field(default_factory=lambda: new_id("q"))
    question: str
    why_it_matters: str = ""
    blocks: list[str] = Field(
        default_factory=list, description="Pipeline stages blocked until answered."
    )
    severity: Severity = Severity.MEDIUM
    raised_by: str = ""
    spans: list[TranscriptSpan] = Field(default_factory=list)
    answer: str = ""
    answered_at: datetime | None = None

    @property
    def answered(self) -> bool:
        return bool(self.answer.strip())


class MeetingMinutes(Contract):
    """The MoM artifact. Rendered to Markdown by
    ``app.services.mom.render_minutes`` and included in the delivery bundle."""

    id: str = Field(default_factory=lambda: new_id("mom"))
    project_id: str
    source_file_id: str = ""
    title: str = "Stakeholder Requirements Meeting"
    meeting_date: str = ""
    duration_hint: str = ""
    transcript_line_count: int = 0

    attendees: list[Attendee] = Field(default_factory=list)
    agenda: list[str] = Field(default_factory=list)
    executive_summary: str = ""
    discussion_points: list[DiscussionPoint] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)

    provenance: Provenance
    generated_at: datetime = Field(default_factory=utcnow)

    @property
    def unassigned_action_items(self) -> list[ActionItem]:
        return [a for a in self.action_items if not a.owner.strip()]


# --------------------------------------------------------------------------
# Requirements specification
# --------------------------------------------------------------------------
class Grain(str, Enum):
    TRANSACTION = "transaction"
    DAILY = "daily"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"
    SNAPSHOT = "snapshot"
    UNKNOWN = "unknown"


class AggregationKind(str, Enum):
    SUM = "sum"
    AVERAGE = "average"
    COUNT = "count"
    DISTINCT_COUNT = "distinct_count"
    MIN = "min"
    MAX = "max"
    RATIO = "ratio"
    VARIANCE = "variance"
    PERCENT_OF_TOTAL = "percent_of_total"
    RUNNING_TOTAL = "running_total"
    PERIOD_CHANGE = "period_change"
    NON_ADDITIVE = "non_additive"


class KpiRequirement(Contract):
    """One requested KPI, expressed in business terms. The DAX Agent turns
    this -- and only this -- into a measure; it may not invent KPIs."""

    id: str = Field(default_factory=lambda: new_id("kpi"))
    name: str
    business_question: str = Field(
        default="", description="The question the KPI answers; drives visual choice."
    )
    definition: str = Field(
        default="",
        description="The agreed business definition. Empty => an OpenQuestion is raised "
        "rather than a definition being guessed.",
    )
    aggregation: AggregationKind = AggregationKind.SUM
    numerator_hint: str = ""
    denominator_hint: str = ""
    unit: str = Field(default="", description="e.g. USD, hours, %, count")
    format_string: str = Field(default="", description="Power BI format string.")
    required_dimensions: list[str] = Field(default_factory=list)
    time_intelligence: list[str] = Field(
        default_factory=list, description="e.g. ['ytd', 'mom'] -- only honoured if a date dim exists."
    )
    higher_is_better: bool | None = None
    target: float | None = None
    priority: int = Field(default=2, ge=1, le=3, description="1 = must have.")
    source_decisions: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.MEDIUM
    needs_confirmation: bool = False


class DimensionRequirement(Contract):
    name: str
    purpose: str = ""
    as_slicer: bool = False
    hierarchy: list[str] = Field(default_factory=list)


class MetricDefinition(Contract):
    """A shared business term, e.g. 'Active Project' or 'Burdened Cost'.
    Conflicting definitions across sources are recorded as conflicts, not
    silently reconciled."""

    term: str
    definition: str
    conflicts_with: list[str] = Field(default_factory=list)
    agreed: bool = False


class Assumption(Contract):
    id: str = Field(default_factory=lambda: new_id("asm"))
    statement: str
    impact_if_wrong: str = ""
    severity: Severity = Severity.MEDIUM
    confirmed: bool | None = Field(
        default=None, description="None = awaiting user; True/False once answered."
    )
    made_by: str = ""


class RequirementsSpec(Contract):
    """The Step-3 deliverable. Every later stage reads its goals from here."""

    id: str = Field(default_factory=lambda: new_id("req"))
    project_id: str
    version: int = 1

    business_objective: str = ""
    objectives: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)

    kpis: list[KpiRequirement] = Field(default_factory=list)
    dimensions: list[DimensionRequirement] = Field(default_factory=list)
    definitions: list[MetricDefinition] = Field(default_factory=list)

    reporting_period_start: str = ""
    reporting_period_end: str = ""
    expected_grain: Grain = Grain.UNKNOWN
    currency: str = ""
    units: str = ""
    audiences: list[str] = Field(default_factory=list)
    refresh_cadence: str = ""

    assumptions: list[Assumption] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    derived_from_minutes: str = Field(default="", description="MeetingMinutes id, if any.")

    provenance: Provenance
    generated_at: datetime = Field(default_factory=utcnow)
    approved: bool = False
    approved_at: datetime | None = None

    def kpi(self, name: str) -> KpiRequirement | None:
        lowered = name.strip().lower()
        return next((k for k in self.kpis if k.name.strip().lower() == lowered), None)

    @property
    def blocking_questions(self) -> list[OpenQuestion]:
        return [
            q for q in self.open_questions
            if not q.answered and q.severity in {Severity.CRITICAL, Severity.HIGH}
        ]


class RequirementsAgentInput(Contract):
    project_id: str
    project_name: str
    description: str = ""
    business_objective: str = ""
    stated_kpis: list[str] = Field(default_factory=list)
    reporting_questions: list[str] = Field(default_factory=list)
    reporting_period: str = ""
    currency: str = ""
    units: str = ""
    business_definitions: dict[str, str] = Field(default_factory=dict)
    transcript_text: str = ""
    transcript_source_file_id: str = ""
    design_preferences: str = ""
    available_tables: list[str] = Field(
        default_factory=list, description="Profiled table names, so KPIs can be grounded."
    )
    available_columns: dict[str, list[str]] = Field(default_factory=dict)


class RequirementsAgentOutput(Contract):
    spec: RequirementsSpec
    minutes: MeetingMinutes | None = None
    notes: list[str] = Field(default_factory=list)
