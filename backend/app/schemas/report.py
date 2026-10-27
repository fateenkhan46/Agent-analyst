"""Contracts for Step 7: the Report Design Agent.

A ``VisualDefinition`` must bind to fields that exist in the approved
``SemanticModel``; ``app.services.pbip.report`` refuses to emit a binding it
cannot resolve, which is what stops a generated report opening with broken
visuals in Power BI Desktop.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from .common import Contract, Issue, Provenance, new_id, utcnow


class VisualType(str, Enum):
    """Restricted to core visuals whose PBIR representation we emit and test."""

    CARD = "card"
    KPI = "kpi"
    MULTI_ROW_CARD = "multiRowCard"
    CLUSTERED_COLUMN = "clusteredColumnChart"
    CLUSTERED_BAR = "clusteredBarChart"
    STACKED_COLUMN = "stackedColumnChart"
    LINE = "lineChart"
    LINE_CLUSTERED_COLUMN = "lineClusteredColumnComboChart"
    AREA = "areaChart"
    DONUT = "donutChart"
    TABLE = "tableEx"
    MATRIX = "pivotTable"
    SLICER = "slicer"
    GAUGE = "gauge"
    SCATTER = "scatterChart"
    WATERFALL = "waterfallChart"
    TEXT_BOX = "textbox"


class FieldRole(str, Enum):
    VALUES = "Values"
    CATEGORY = "Category"
    SERIES = "Series"
    AXIS = "Axis"
    ROWS = "Rows"
    COLUMNS = "Columns"
    TOOLTIPS = "Tooltips"
    TARGET = "TargetValue"
    TREND = "TrendLine"
    FILTER = "Filter"
    SIZE = "Size"


class FieldBinding(Contract):
    role: FieldRole
    table: str
    field: str = Field(description="Column or measure name.")
    is_measure: bool = False
    display_name: str = ""
    format_string: str = ""
    aggregation: str = Field(
        default="", description="Only for columns; measures carry their own aggregation."
    )
    sort_direction: str = Field(default="", description="asc | desc | ''")

    @property
    def reference(self) -> str:
        return f"{self.table}[{self.field}]"


class Layout(Contract):
    x: float = 0
    y: float = 0
    width: float = 320
    height: float = 220
    z: int = 0


class VisualDefinition(Contract):
    id: str = Field(default_factory=lambda: new_id("vis"))
    name: str = ""
    visual_type: VisualType
    title: str = ""
    subtitle: str = ""
    layout: Layout = Field(default_factory=Layout)
    bindings: list[FieldBinding] = Field(default_factory=list)

    business_question: str = Field(
        default="", description="The question this visual answers."
    )
    design_rationale: str = Field(
        default="", description="Why this visual type suits that question."
    )
    kpi_ids: list[str] = Field(default_factory=list)

    show_data_labels: bool = False
    show_legend: bool = True
    drill_through_target: str = ""
    drill_down_enabled: bool = False
    tooltip_fields: list[FieldBinding] = Field(default_factory=list)
    interactions_disabled_with: list[str] = Field(default_factory=list)

    @property
    def measure_references(self) -> list[str]:
        return [b.reference for b in self.bindings if b.is_measure]


class PageFilter(Contract):
    table: str
    field: str
    operator: str = Field(default="in", description="in | between | top_n | relative_date")
    values: list[str] = Field(default_factory=list)
    description: str = ""


class ReportPage(Contract):
    id: str = Field(default_factory=lambda: new_id("page"))
    name: str = Field(description="Internal page name (used as the PBIR folder).")
    display_name: str
    ordinal: int = 0
    purpose: str = ""
    audience: str = ""
    width: int = 1280
    height: int = 720
    visuals: list[VisualDefinition] = Field(default_factory=list)
    page_filters: list[PageFilter] = Field(default_factory=list)
    is_drill_through: bool = False
    drill_through_field: str = ""
    is_hidden: bool = False


class Theme(Contract):
    name: str = "AgentAnalystTheme"
    foreground: str = "#1A1F2B"
    background: str = "#FFFFFF"
    background_light: str = "#F4F6FA"
    table_accent: str = "#2E6BE6"
    data_colors: list[str] = Field(
        default_factory=lambda: [
            "#2E6BE6", "#17A398", "#F2A541", "#D1495B",
            "#6A5ACD", "#3C9D4E", "#B07C9E", "#5A6978",
        ]
    )
    font_family: str = "Segoe UI"
    positive_color: str = "#17A398"
    negative_color: str = "#D1495B"
    neutral_color: str = "#5A6978"


class ReportSpec(Contract):
    """The Step-7 deliverable."""

    id: str = Field(default_factory=lambda: new_id("rpt"))
    project_id: str
    model_id: str
    name: str = "Report"
    version: int = 1
    pages: list[ReportPage] = Field(default_factory=list)
    theme: Theme = Field(default_factory=Theme)
    design_notes: str = ""
    unmapped_kpis: list[str] = Field(
        default_factory=list,
        description="KPIs with no visual -- reported rather than padded with filler visuals.",
    )
    issues: list[Issue] = Field(default_factory=list)
    provenance: Provenance | None = None
    generated_at: datetime = Field(default_factory=utcnow)
    approved: bool = False

    @property
    def visual_count(self) -> int:
        return sum(len(p.visuals) for p in self.pages)

    def all_visuals(self) -> list[VisualDefinition]:
        return [v for p in self.pages for v in p.visuals]


class ExportKind(str, Enum):
    PBIP = "pbip"
    TMDL_ONLY = "tmdl"
    MOM_MARKDOWN = "mom_markdown"
    DELIVERY_BUNDLE = "delivery_bundle"
    PBIX = "pbix"


class ExportArtifact(Contract):
    """Record of a file that **actually exists** on disk. Created only after a
    successful write; ``verified_at`` is set by re-reading the file."""

    id: str = Field(default_factory=lambda: new_id("exp"))
    project_id: str
    kind: ExportKind
    filename: str
    stored_path: str
    size_bytes: int = 0
    sha256: str = ""
    file_count: int = 0
    manifest: list[str] = Field(default_factory=list, description="Paths inside the archive.")
    created_at: datetime = Field(default_factory=utcnow)
    verified_at: datetime | None = None
    remaining_manual_steps: list[str] = Field(default_factory=list)
    opens_in: str = Field(default="", description="e.g. 'Power BI Desktop (July 2024+)'")


class ReportAgentInput(Contract):
    project_id: str
    model_id: str
    requirements_id: str = ""
    design_preferences: str = ""
    max_pages: int = 5
    qa_feedback: list[str] = Field(default_factory=list)


class ReportAgentOutput(Contract):
    report: ReportSpec
    notes: list[str] = Field(default_factory=list)
