"""Typed contracts for every artifact and every inter-agent message.

JSON Schema for all of them is served at ``GET /api/v1/system/contracts``,
which is also what the test-suite snapshots to detect accidental breaking
changes to an agent boundary.
"""
from .common import (  # noqa: F401
    Capability, CapabilityStatus, CanonicalType, Confidence, Contract, Issue,
    Money, Origin, Provenance, Severity, new_id, utcnow,
)
from .source import (  # noqa: F401
    ColumnProfile, FormatVariant, IngestStatus, KeyOverlap, NumericStats, Outlier,
    SourceDataProfile, SourceFile, SourceKind, SourceTable, TemporalStats,
)
from .requirements import (  # noqa: F401
    ActionItem, AggregationKind, Assumption, Attendee, Decision, DimensionRequirement,
    DiscussionPoint, Grain, KpiRequirement, MeetingMinutes, MetricDefinition, OpenQuestion,
    RequirementsAgentInput, RequirementsAgentOutput, RequirementsSpec, TranscriptSpan,
)
from .prep import (  # noqa: F401
    ColumnMapping, DataQualityReport, DuplicateFinding, JoinProposal, LineageEdge,
    PrepAgentInput, PrepAgentOutput, PreparedDataset, Risk, RowCountDelta,
    TransformOp, TransformationExecution, TransformationPlan, TransformationStep,
)
from .semantic import (  # noqa: F401
    Cardinality, ColumnDefinition, CrossFilter, DaxAgentInput, DaxAgentOutput,
    DiagramEdge, DiagramNode, MeasureDefinition, MeasureValidationCase, ModelDiagram,
    RelationshipDefinition, SemanticModel, SemanticModelAgentInput,
    SemanticModelAgentOutput, SummarizeBy, TableDefinition, TableRole,
)
from .report import (  # noqa: F401
    ExportArtifact, ExportKind, FieldBinding, FieldRole, Layout, PageFilter,
    ReportAgentInput, ReportAgentOutput, ReportPage, ReportSpec, Theme,
    VisualDefinition, VisualType,
)
from .agent import (  # noqa: F401
    AgentExecution, AgentName, AgentPolicy, AgentSpec, AgentTask, ApprovalKind,
    ApprovalRequest, ApprovalState, AuditAction, AuditEvent, ExecutionStatus,
    LlmCallRecord, TASK_DEPENDENCIES, TASK_OWNER, TaskGraph, TaskKind, TaskStatus,
    ToolCallRecord,
)
from .qa import (  # noqa: F401
    ImprovementAgentInput, ImprovementAgentOutput, ImprovementArea, ImprovementRecommendation,
    ImprovementReport, QaAgentInput, QaAgentOutput, ReworkRequest, TestCategory, TestStatus,
    ValidationEngine, ValidationReport, ValidationResult, ValidationTest,
)
from .project import (  # noqa: F401
    DeliveryBundle, DesignPreferences, Project, ProjectCreate, ProjectStatus,
)

#: The 20+ contracts the brief requires, by name, so tests can assert coverage.
REQUIRED_CONTRACTS = [
    "Project", "SourceFile", "SourceTable", "ColumnProfile", "BusinessRequirement",
    "TransformationPlan", "TransformationStep", "SemanticModel", "TableDefinition",
    "RelationshipDefinition", "MeasureDefinition", "ReportPage", "VisualDefinition",
    "AgentTask", "AgentExecution", "ValidationTest", "ValidationResult",
    "ImprovementRecommendation", "ApprovalRequest", "AuditEvent",
    "MeetingMinutes",
]

#: ``BusinessRequirement`` in the brief maps to our richer pair; alias it so
#: the name resolves and nothing is quietly missing.
BusinessRequirement = KpiRequirement
