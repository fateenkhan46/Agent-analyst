"""Designing the report.

The rule the brief states plainly: no visual exists to fill space. Every
visual here is created because a specific business question needs answering,
binds only to fields that exist in the approved model, and carries the reason
that visual type suits that question.

Page structure follows how the report is actually read:

1. **Executive summary** -- the headline KPIs and the one chart that explains
   them. This is the page the committee opens.
2. **Analysis** -- the breakdowns: by category, by portfolio, with the trend.
3. **Detail** -- a table at transaction grain, for someone checking a number.
4. **Project detail (drill-through)** -- reached by right-clicking a project,
   not navigated to.
5. **Data quality** -- what the data could not tell us. Included because a
   report that hides its own caveats gets trusted more than it deserves.

A KPI with no measure gets no visual, and is listed in ``unmapped_kpis`` so the
gap is visible rather than papered over.
"""
from __future__ import annotations

from typing import Any

from ...schemas.common import Confidence, Issue, Origin, Provenance, Severity
from ...schemas.report import (
    ExportKind, FieldBinding, FieldRole, Layout, PageFilter, ReportPage, ReportSpec,
    Theme, VisualDefinition, VisualType,
)
from ...schemas.requirements import AggregationKind, KpiRequirement, RequirementsSpec
from ...schemas.semantic import MeasureDefinition, SemanticModel, TableRole
from .. import kpi_catalog

#: Canvas is 1280x720; this grid keeps visuals aligned without a layout engine.
GUTTER = 16.0
CARD_HEIGHT = 110.0
ROW_HEIGHT = 250.0


def _grid(columns: int, index: int, *, y: float, height: float,
          width_total: float = 1280.0) -> Layout:
    usable = width_total - GUTTER * (columns + 1)
    width = usable / columns
    return Layout(
        x=round(GUTTER + index * (width + GUTTER), 1), y=y,
        width=round(width, 1), height=height,
    )


def _measure_binding(
    measure: MeasureDefinition, *, role: FieldRole = FieldRole.VALUES
) -> FieldBinding:
    return FieldBinding(
        role=role, table=measure.home_table or "", field=measure.name,
        is_measure=True, display_name=measure.name,
        format_string=measure.format_string,
    )


def _column_binding(
    table: str, column: str, role: FieldRole, *, display: str = "",
    sort: str = "",
) -> FieldBinding:
    return FieldBinding(
        role=role, table=table, field=column, is_measure=False,
        display_name=display or column.replace("_", " ").title(),
        sort_direction=sort,
    )


def _find_attribute(
    model: SemanticModel, roles: set[str], *, allow_date_table: bool = False
) -> tuple[str, str] | None:
    """A visible dimension column whose semantic role is in ``roles``.

    The date table is excluded unless asked for: its 'day_name' column has the
    'name' role, and without this guard a request for the project name picks up
    a weekday instead -- which looks plausible on screen and is nonsense.
    """
    candidates: list[tuple[str, str]] = []
    for table in model.tables:
        if table.role is TableRole.DATE and not allow_date_table:
            continue
        if table.role not in {TableRole.DIMENSION, TableRole.DATE}:
            continue
        for column in table.columns:
            if column.is_hidden:
                continue
            if kpi_catalog.role_for_column(column.name) in roles:
                candidates.append((table.name, column.name))
    return candidates[0] if candidates else None


def _find_month(model: SemanticModel) -> tuple[str, str, str] | None:
    """(table, label column, sort column) for a monthly axis."""
    for table in model.date_tables:
        label = next((c.name for c in table.columns if c.name == "month_year"), "")
        sort = next((c.name for c in table.columns if c.name == "month_key"), "")
        if label and sort:
            return table.name, label, sort
    return None


def design_report(
    *, project_id: str, model: Any, requirements: Any = None,
    design_preferences: str = "", max_pages: int = 5,
    qa_feedback: list[str] | None = None,
) -> dict[str, Any]:
    model = model if isinstance(model, SemanticModel) else SemanticModel.model_validate(model)
    spec: RequirementsSpec | None = None
    if requirements is not None:
        spec = (
            requirements if isinstance(requirements, RequirementsSpec)
            else RequirementsSpec.model_validate(requirements)
        )

    if not model.measures:
        return {
            "report": ReportSpec(
                project_id=project_id, model_id=model.id, name="Report",
                issues=[Issue(
                    code="report.no_measures", severity=Severity.CRITICAL,
                    location="model",
                    message="The model has no measures, so no report was designed.",
                    recommended_fix="Generate measures first; a report of bare columns "
                                    "would not answer any of the reporting questions.",
                )],
                provenance=Provenance(
                    origin=Origin.DETERMINISTIC, produced_by="report_design_agent",
                    rationale="No measures were available to bind to.",
                ),
            ),
            "notes": ["No measures exist, so no report was designed."],
        }

    by_name = {m.name.lower(): m for m in model.measures}
    kpis: list[KpiRequirement] = spec.kpis if spec else []
    mapped: dict[str, MeasureDefinition] = {}
    unmapped: list[str] = []
    for kpi in kpis:
        measure = by_name.get(kpi.name.lower())
        if measure is None:
            unmapped.append(kpi.name)
        else:
            mapped[kpi.name] = measure
    # measures with no matching KPI are still usable
    for measure in model.measures:
        if measure.name not in mapped:
            mapped.setdefault(measure.name, measure)

    theme = _theme_from(design_preferences)
    pages: list[ReportPage] = []
    notes: list[str] = list(qa_feedback or [])

    headline = _headline_measures(mapped, kpis)
    pages.append(_executive_page(model, mapped, headline, kpis, spec))
    analysis = _analysis_page(model, mapped, headline, kpis)
    if analysis is not None:
        pages.append(analysis)
    detail = _detail_page(model, mapped, headline)
    if detail is not None:
        pages.append(detail)
    drill = _drill_through_page(model, mapped, headline)
    if drill is not None:
        pages.append(drill)
    quality = _data_quality_page(model, spec)
    if quality is not None:
        pages.append(quality)

    pages = pages[: max(1, max_pages)]
    for i, page in enumerate(pages):
        page.ordinal = i

    # A KPI is "unmapped" when no visual reports it -- whether that is because
    # it has no measure, or because it has one that no page happened to use.
    # Tracking only the first case let a measure with no visual fall through
    # both checks, so a requested KPI could silently never appear.
    bound_measures = {
        binding.field
        for page in pages for visual in page.visuals for binding in visual.bindings
        if binding.is_measure
    }
    unmapped = [
        kpi.name for kpi in kpis
        if (mapped.get(kpi.name).name if mapped.get(kpi.name) else None)
        not in bound_measures
    ] if kpis else unmapped

    report = ReportSpec(
        project_id=project_id, model_id=model.id,
        name=(spec.business_objective[:48] if spec and spec.business_objective
              else "Portfolio Report"),
        pages=pages, theme=theme, unmapped_kpis=unmapped,
        design_notes=_design_notes(pages, unmapped, model),
        provenance=Provenance(
            origin=Origin.DETERMINISTIC, produced_by="report_design_agent",
            confidence=Confidence.HIGH,
            rationale="Each visual answers a stated reporting question and binds only to "
                      "measures and columns present in the approved model. No visual was "
                      "added to fill space.",
            evidence=[
                f"{len(pages)} page(s), "
                f"{sum(len(p.visuals) for p in pages)} visual(s)",
                f"{len(mapped)} measure(s) bound, {len(unmapped)} KPI(s) unmapped",
            ],
        ),
    )
    report.issues = _validate_report(report, model)
    if unmapped:
        without_measure = [k for k in unmapped if k not in mapped]
        without_visual = [k for k in unmapped if k in mapped]
        if without_measure:
            notes.append(
                f"{len(without_measure)} requested KPI(s) have no measure, so no visual "
                f"could report them: " + ", ".join(without_measure) + "."
            )
        if without_visual:
            notes.append(
                f"{len(without_visual)} KPI(s) have a measure but no visual on any page: "
                + ", ".join(without_visual)
                + ". They are available in the field list and listed on the report "
                  "rather than being omitted silently."
            )
    return {"report": report, "notes": notes}


def _theme_from(preferences: str) -> Theme:
    theme = Theme()
    import re
    colours = re.findall(r"#[0-9A-Fa-f]{6}", preferences or "")
    if colours:
        theme.table_accent = colours[0]
        theme.data_colors = colours + theme.data_colors[len(colours):]
    low = (preferences or "").lower()
    if "dark" in low:
        theme.background = "#1A1F2B"
        theme.foreground = "#F4F6FA"
        theme.background_light = "#242B38"
    return theme


def _headline_measures(
    mapped: dict[str, MeasureDefinition], kpis: list[KpiRequirement]
) -> dict[str, MeasureDefinition]:
    """Resolve the well-known roles a layout needs, by catalogue shape."""
    out: dict[str, MeasureDefinition] = {}
    for name, measure in mapped.items():
        shape = kpi_catalog.match(name)
        if shape and shape.key not in out:
            out[shape.key] = measure
    return out


def _executive_page(
    model: SemanticModel, mapped: dict[str, MeasureDefinition],
    headline: dict[str, MeasureDefinition], kpis: list[KpiRequirement],
    spec: RequirementsSpec | None,
) -> ReportPage:
    page = ReportPage(
        name="ExecutiveSummary", display_name="Executive Summary",
        purpose="The headline numbers the committee asks for, and the one chart that "
                "explains them.",
        audience=", ".join(spec.audiences) if spec and spec.audiences else "Executive",
    )

    # KPI cards: the must-have KPIs first, capped at four so the row stays readable.
    priority = ["total_cost", "total_budget", "budget_variance", "budget_variance_pct",
                "total_labour_hours", "average_cost_per_project"]
    cards = [headline[k] for k in priority if k in headline][:4]
    if not cards:
        cards = list(mapped.values())[:4]
    for i, measure in enumerate(cards):
        kpi = next((k for k in kpis if k.name == measure.name), None)
        question = (
            kpi.business_question if kpi and kpi.business_question
            else f"What is the current {measure.name.lower()}?"
        )
        page.visuals.append(VisualDefinition(
            name=f"card_{i}", visual_type=VisualType.CARD, title=measure.name,
            subtitle=measure.unit,
            layout=_grid(len(cards), i, y=GUTTER, height=CARD_HEIGHT),
            bindings=[_measure_binding(measure)],
            business_question=question,
            design_rationale="A card is the right visual for a single headline number: "
                             "no axis or legend competes with the value, and it is the "
                             "first thing read on the page.",
            kpi_ids=[kpi.id] if kpi else [],
            show_data_labels=True, show_legend=False,
        ))

    y = GUTTER + CARD_HEIGHT + GUTTER
    month = _find_month(model)
    cost = headline.get("total_cost")

    if month and cost:
        table, label, sort = month
        bindings = [
            _column_binding(table, label, FieldRole.AXIS, display="Month", sort="asc"),
            _measure_binding(cost),
        ]
        budget = headline.get("total_budget")
        combo_type = VisualType.LINE
        rationale = (
            "A line chart over a continuous month axis shows the shape of the trend -- "
            "whether cost is accelerating -- which a bar chart of the same data hides."
        )
        if budget:
            bindings.append(_measure_binding(budget, role=FieldRole.VALUES))
            combo_type = VisualType.LINE_CLUSTERED_COLUMN
            rationale = (
                "Columns for monthly cost against a line for budget puts the two on one "
                "axis, so over- and under-spend is read directly rather than inferred "
                "from two charts."
            )
        page.visuals.append(VisualDefinition(
            name="trend", visual_type=combo_type,
            title=f"{cost.name} by Month",
            layout=_grid(2, 0, y=y, height=ROW_HEIGHT),
            bindings=bindings,
            business_question="What is the monthly cost trend, and is it accelerating?",
            design_rationale=rationale,
            show_legend=True, drill_down_enabled=True,
        ))

    portfolio = _find_attribute(model, {"portfolio"})
    variance = headline.get("budget_variance")
    if portfolio and (variance or cost):
        measure = variance or cost
        assert measure is not None
        page.visuals.append(VisualDefinition(
            name="by_portfolio", visual_type=VisualType.CLUSTERED_BAR,
            title=f"{measure.name} by {portfolio[1].replace('_', ' ').title()}",
            layout=_grid(2, 1, y=y, height=ROW_HEIGHT),
            bindings=[
                _column_binding(portfolio[0], portfolio[1], FieldRole.CATEGORY,
                                sort="desc"),
                _measure_binding(measure),
            ],
            business_question="Which portfolios are over budget, and by how much?",
            design_rationale="A horizontal bar chart sorts categories by value and keeps "
                             "long portfolio names readable, which a column chart does "
                             "not. Variance can be negative, so the bars extend both "
                             "ways from zero.",
            show_data_labels=True, show_legend=False,
        ))

    # Slicers for the dimensions the requirements asked to filter by.
    y2 = y + ROW_HEIGHT + GUTTER
    slicer_roles = [("status", {"status"}), ("portfolio", {"portfolio"})]
    slicers = []
    for role_name, roles in slicer_roles:
        found = _find_attribute(model, roles)
        if found:
            slicers.append((role_name, found))
    for i, (role_name, (table, column)) in enumerate(slicers[:3]):
        page.visuals.append(VisualDefinition(
            name=f"slicer_{role_name}", visual_type=VisualType.SLICER,
            title=column.replace("_", " ").title(),
            layout=_grid(max(3, len(slicers)), i, y=y2, height=140.0),
            bindings=[_column_binding(table, column, FieldRole.CATEGORY)],
            business_question=f"Let the reader restrict every number to one "
                              f"{column.replace('_', ' ')}.",
            design_rationale="A slicer on a conformed dimension filters every fact table "
                             "consistently; a slicer on a fact column would filter only "
                             "that table and quietly leave the other totals unfiltered.",
            show_legend=False,
        ))
    return page


def _analysis_page(
    model: SemanticModel, mapped: dict[str, MeasureDefinition],
    headline: dict[str, MeasureDefinition], kpis: list[KpiRequirement],
) -> ReportPage | None:
    cost = headline.get("total_cost")
    if cost is None:
        return None
    page = ReportPage(
        name="CostAnalysis", display_name="Cost Analysis",
        purpose="Where the cost goes: by category and portfolio, with the trend.",
        audience="Portfolio managers",
    )
    category = _find_attribute(model, {"category"})
    portfolio = _find_attribute(model, {"portfolio"})
    manager = _find_attribute(model, {"manager"})

    index = 0
    if category:
        page.visuals.append(VisualDefinition(
            name="by_category", visual_type=VisualType.CLUSTERED_COLUMN,
            title=f"{cost.name} by {category[1].replace('_', ' ').title()}",
            layout=_grid(2, index, y=GUTTER, height=ROW_HEIGHT),
            bindings=[
                _column_binding(category[0], category[1], FieldRole.AXIS, sort="desc"),
                _measure_binding(cost),
            ],
            business_question="How does cost split by category?",
            design_rationale="A column chart compares a handful of unordered categories "
                             "directly. A pie chart of the same data would make the "
                             "comparison harder, which is why one is not used.",
            show_data_labels=True, show_legend=False,
        ))
        index += 1
    if portfolio and index < 2:
        page.visuals.append(VisualDefinition(
            name="cost_by_portfolio", visual_type=VisualType.CLUSTERED_BAR,
            title=f"{cost.name} by {portfolio[1].replace('_', ' ').title()}",
            layout=_grid(2, index, y=GUTTER, height=ROW_HEIGHT),
            bindings=[
                _column_binding(portfolio[0], portfolio[1], FieldRole.CATEGORY, sort="desc"),
                _measure_binding(cost),
            ],
            business_question="Which portfolios consume the most cost?",
            design_rationale="Sorted bars rank the portfolios, which is the actual "
                             "question being asked.",
            show_data_labels=True, show_legend=False,
        ))
        index += 1

    y = GUTTER + ROW_HEIGHT + GUTTER
    month = _find_month(model)
    hours = headline.get("total_labour_hours")
    if month and portfolio and hours:
        table, label, sort = month
        page.visuals.append(VisualDefinition(
            name="hours_matrix", visual_type=VisualType.MATRIX,
            title=f"{hours.name} by Portfolio and Month",
            layout=_grid(2, 0, y=y, height=ROW_HEIGHT),
            bindings=[
                _column_binding(portfolio[0], portfolio[1], FieldRole.ROWS),
                _column_binding(table, label, FieldRole.COLUMNS, sort="asc"),
                _measure_binding(hours),
            ],
            business_question="Which portfolios consume the most labour hours, and when?",
            design_rationale="A matrix is the right shape for two crossed dimensions with "
                             "one measure; a chart with twelve month series would be "
                             "unreadable.",
            show_legend=False, drill_down_enabled=True,
        ))
    variance_pct = headline.get("budget_variance_pct")
    if manager and (variance_pct or cost):
        measure = variance_pct or cost
        assert measure is not None
        page.visuals.append(VisualDefinition(
            name="by_manager", visual_type=VisualType.TABLE,
            title=f"{measure.name} by {manager[1].replace('_', ' ').title()}",
            layout=_grid(2, 1, y=y, height=ROW_HEIGHT),
            bindings=[
                _column_binding(manager[0], manager[1], FieldRole.ROWS),
                _measure_binding(measure),
            ] + ([_measure_binding(cost)] if cost and measure is not cost else []),
            business_question="How does performance vary by project manager?",
            design_rationale="A table shows the exact figures per manager, which is what "
                             "a reader checking accountability wants rather than a chart "
                             "they have to read values off.",
            show_legend=False,
        ))
    return page if page.visuals else None


def _detail_page(
    model: SemanticModel, mapped: dict[str, MeasureDefinition],
    headline: dict[str, MeasureDefinition],
) -> ReportPage | None:
    fact = next((t for t in model.fact_tables), None)
    if fact is None:
        return None
    project = _find_attribute(model, {"project_key"}) or _find_attribute(model, {"name"})
    page = ReportPage(
        name="ProjectDetail", display_name="Project Detail",
        purpose="A row per project with every headline measure, for someone checking a "
                "number.",
        audience="Analysts",
    )
    bindings: list[FieldBinding] = []
    if project:
        bindings.append(_column_binding(project[0], project[1], FieldRole.ROWS))
    name = _find_attribute(model, {"name"})
    if name and name != project:
        bindings.append(_column_binding(name[0], name[1], FieldRole.ROWS))
    status = _find_attribute(model, {"status"})
    if status:
        bindings.append(_column_binding(status[0], status[1], FieldRole.ROWS))
    for key in ("total_budget", "total_cost", "budget_variance", "budget_variance_pct",
                "total_labour_hours", "project_completion_pct"):
        measure = headline.get(key)
        if measure is not None:
            bindings.append(_measure_binding(measure))
    if len(bindings) < 2:
        return None
    page.visuals.append(VisualDefinition(
        name="project_table", visual_type=VisualType.TABLE,
        title="Projects",
        layout=Layout(x=GUTTER, y=GUTTER, width=1248.0, height=640.0),
        bindings=bindings,
        business_question="What are the exact figures for each project?",
        design_rationale="A table at project grain is where a reader reconciles the "
                         "headline numbers. Every measure on the executive page appears "
                         "here as a column, so a disputed total can be traced to the "
                         "project that drives it.",
        show_legend=False,
        drill_through_target="ProjectDrillThrough",
    ))
    return page


def _drill_through_page(
    model: SemanticModel, mapped: dict[str, MeasureDefinition],
    headline: dict[str, MeasureDefinition],
) -> ReportPage | None:
    project = _find_attribute(model, {"project_key"})
    fact = next((t for t in model.fact_tables), None)
    if project is None or fact is None:
        return None
    page = ReportPage(
        name="ProjectDrillThrough", display_name="Single Project",
        purpose="Everything about one project, reached by drilling through from a table "
                "or chart rather than navigated to directly.",
        audience="Portfolio managers",
        is_drill_through=True,
        drill_through_field=f"{project[0]}[{project[1]}]",
        is_hidden=True,
    )
    cards = [
        headline[k] for k in
        ("total_cost", "total_budget", "budget_variance", "total_labour_hours")
        if k in headline
    ][:4]
    for i, measure in enumerate(cards):
        page.visuals.append(VisualDefinition(
            name=f"dt_card_{i}", visual_type=VisualType.CARD, title=measure.name,
            layout=_grid(max(1, len(cards)), i, y=GUTTER, height=CARD_HEIGHT),
            bindings=[_measure_binding(measure)],
            business_question=f"What is this project's {measure.name.lower()}?",
            design_rationale="The same cards as the executive page, so a reader moving "
                             "from the portfolio view to one project sees the identical "
                             "measures rather than having to re-interpret new ones.",
            show_data_labels=True, show_legend=False,
        ))
    month = _find_month(model)
    cost = headline.get("total_cost")
    if month and cost:
        table, label, sort = month
        page.visuals.append(VisualDefinition(
            name="dt_trend", visual_type=VisualType.LINE,
            title="Monthly cost for this project",
            layout=Layout(x=GUTTER, y=GUTTER + CARD_HEIGHT + GUTTER,
                          width=1248.0, height=240.0),
            bindings=[
                _column_binding(table, label, FieldRole.AXIS, display="Month", sort="asc"),
                _measure_binding(cost),
            ],
            business_question="When did this project spend?",
            design_rationale="The project-level trend is what explains an unusual total "
                             "on the portfolio view.",
            show_legend=False,
        ))
    category = _find_attribute(model, {"category"})
    if category and cost:
        page.visuals.append(VisualDefinition(
            name="dt_category", visual_type=VisualType.CLUSTERED_BAR,
            title="Cost by category for this project",
            layout=Layout(x=GUTTER, y=GUTTER + CARD_HEIGHT + 240.0 + GUTTER * 2,
                          width=620.0, height=240.0),
            bindings=[
                _column_binding(category[0], category[1], FieldRole.CATEGORY, sort="desc"),
                _measure_binding(cost),
            ],
            business_question="What is this project spending on?",
            design_rationale="Breaks the single project's cost into its categories, which "
                             "is the first question asked of an overspend.",
            show_data_labels=True, show_legend=False,
        ))
    return page if page.visuals else None


def _data_quality_page(
    model: SemanticModel, spec: RequirementsSpec | None
) -> ReportPage | None:
    """A page for what the data could not tell us.

    Included deliberately: a report that hides its own caveats gets trusted
    more than it deserves. The unmatched-key and currency findings belong in
    front of the reader, not only in a QA log.
    """
    concerns = [i for i in model.issues if i.severity in {
        Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM
    }]
    unresolved = [q for q in (spec.open_questions if spec else []) if not q.answered]
    if not concerns and not unresolved:
        return None

    page = ReportPage(
        name="DataNotes", display_name="Data Notes",
        purpose="Caveats a reader needs in order to trust the numbers: unmatched keys, "
                "excluded rows and unresolved definitions.",
        audience="All readers", is_hidden=False,
    )
    lines = ["## What to know about these numbers", ""]
    for issue in concerns[:8]:
        lines.append(f"- **{issue.location}**: {issue.message}")
        if issue.recommended_fix:
            lines.append(f"  - {issue.recommended_fix}")
    if unresolved:
        lines.append("")
        lines.append("## Questions still open")
        for question in unresolved[:6]:
            lines.append(f"- {question.question}")
    unknown_members = [
        f"{r.from_table}: {r.orphan_key_count} row group(s) with no matching "
        f"{r.to_table} record"
        for r in model.relationships if r.orphan_key_count
    ]
    if unknown_members:
        lines.append("")
        lines.append("## Rows attributed to 'UNASSIGNED'")
        lines.extend(f"- {u}" for u in unknown_members)
        lines.append(
            "These rows are kept so totals reconcile to the source; they appear under "
            "an explicit UNASSIGNED member rather than disappearing from the report."
        )

    page.visuals.append(VisualDefinition(
        name="notes", visual_type=VisualType.TEXT_BOX, title="Data notes",
        layout=Layout(x=GUTTER, y=GUTTER, width=1248.0, height=640.0),
        bindings=[],
        business_question="What should a reader know before trusting these numbers?",
        design_rationale="A text box, not a chart: these are statements, and rendering "
                         "them as data would imply a precision they do not have.",
        subtitle="\n".join(lines),
        show_legend=False,
    ))
    return page


def _validate_report(report: ReportSpec, model: SemanticModel) -> list[Issue]:
    issues: list[Issue] = []
    for page in report.pages:
        for visual in page.visuals:
            if visual.visual_type is VisualType.TEXT_BOX:
                continue
            if not visual.bindings:
                issues.append(Issue(
                    code="report.visual_without_fields", severity=Severity.HIGH,
                    location=f"{page.name}.{visual.name}",
                    message=f"'{visual.title}' has no field bindings.",
                    recommended_fix="A visual with no fields renders empty; remove it or "
                                    "bind a measure.",
                ))
                continue
            for binding in visual.bindings:
                if binding.is_measure:
                    if model.measure(binding.field) is None:
                        issues.append(Issue(
                            code="report.unknown_measure", severity=Severity.CRITICAL,
                            location=f"{page.name}.{visual.name}",
                            message=f"'{visual.title}' binds to measure "
                                    f"'{binding.field}', which is not in the model.",
                            recommended_fix="Generate the measure or remove the visual.",
                        ))
                    continue
                if model.resolve_column(binding.table, binding.field) is None:
                    issues.append(Issue(
                        code="report.unknown_column", severity=Severity.CRITICAL,
                        location=f"{page.name}.{visual.name}",
                        message=f"'{visual.title}' binds to "
                                f"{binding.table}[{binding.field}], which is not in the "
                                f"model.",
                        recommended_fix="Correct the binding; Power BI would show this "
                                        "visual as broken.",
                    ))
            if not any(b.is_measure for b in visual.bindings) and \
                    visual.visual_type is not VisualType.SLICER:
                issues.append(Issue(
                    code="report.visual_without_measure", severity=Severity.MEDIUM,
                    location=f"{page.name}.{visual.name}",
                    message=f"'{visual.title}' has no measure, only columns.",
                    recommended_fix="Add a measure, or make it a slicer if it is meant "
                                    "to filter rather than report.",
                ))
    if not report.pages:
        issues.append(Issue(
            code="report.no_pages", severity=Severity.CRITICAL, location="report",
            message="No report page could be designed from the model.",
            recommended_fix="Check that measures and dimensions exist.",
        ))
    return issues


def _design_notes(
    pages: list[ReportPage], unmapped: list[str], model: SemanticModel
) -> str:
    parts = [
        f"{len(pages)} page(s): "
        + ", ".join(f"{p.display_name} ({len(p.visuals)} visual(s))" for p in pages)
        + ".",
        "Every visual states the business question it answers and why its type suits "
        "that question; none was added to fill space.",
    ]
    drill = [p for p in pages if p.is_drill_through]
    if drill:
        parts.append(
            f"'{drill[0].display_name}' is a drill-through page keyed on "
            f"{drill[0].drill_through_field}: it is reached by right-clicking a project, "
            f"not from the navigation."
        )
    if unmapped:
        parts.append(
            f"{len(unmapped)} requested KPI(s) have no measure, so they have no visual: "
            + ", ".join(unmapped) + "."
        )
    if any(p.name == "DataNotes" for p in pages):
        parts.append(
            "A 'Data Notes' page carries the caveats (unmatched keys, excluded rows, "
            "open definitions) in front of the reader rather than only in the QA log."
        )
    return " ".join(parts)
