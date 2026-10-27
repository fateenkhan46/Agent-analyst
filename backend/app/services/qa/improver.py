"""The Improvement Agent's analysis.

Every recommendation must carry its rationale, the measured evidence behind
it, the expected benefit, and the trade-off. A recommendation without a
trade-off is usually a recommendation nobody has thought through, so the
dataclass requires one.

Nothing here changes an approved artifact. Each recommendation carries a
machine-applicable ``proposed_change`` that is only acted on after the user
approves it, and applying it means re-running the responsible agent so the
change stays auditable.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ...schemas.common import Confidence, Origin, Provenance, Severity
from ...schemas.prep import TransformationExecution, TransformationPlan
from ...schemas.qa import (
    ImprovementArea, ImprovementRecommendation, ImprovementReport, TestStatus,
    ValidationReport,
)
from ...schemas.report import ReportSpec
from ...schemas.requirements import RequirementsSpec
from ...schemas.semantic import SemanticModel, TableRole
from ...schemas.source import SourceDataProfile
from .. import kpi_catalog, repository as repo


def _rec(
    project_id: str, area: ImprovementArea, title: str, *, rationale: str,
    evidence: list[str], benefit: str, trade_offs: list[str],
    effort: str = "medium", severity: Severity = Severity.MEDIUM,
    objects: list[str] | None = None, change: dict[str, Any] | None = None,
    quantified: str = "",
) -> ImprovementRecommendation:
    return ImprovementRecommendation(
        project_id=project_id, area=area, title=title, rationale=rationale,
        evidence=evidence, expected_benefit=benefit, quantified_benefit=quantified,
        trade_offs=trade_offs, effort=effort, severity=severity,
        affected_objects=objects or [], proposed_change=change or {},
        provenance=Provenance(
            origin=Origin.DETERMINISTIC, produced_by="improvement_agent",
            confidence=Confidence.MEDIUM,
            rationale="Derived from measured properties of this project's data, model "
                      "and validation results.",
        ),
    )


def suggest_improvements(**kwargs: Any) -> dict[str, Any]:
    """Entry point used by the heuristic provider and the agent alike."""
    s: Session = kwargs["session"]
    project_id: str = kwargs["project_id"]

    profile = repo.load_artifact(s, project_id, repo.ArtifactKind.PROFILE, SourceDataProfile)
    requirements = repo.load_artifact(
        s, project_id, repo.ArtifactKind.REQUIREMENTS, RequirementsSpec
    )
    plan = repo.load_artifact(s, project_id, repo.ArtifactKind.PLAN, TransformationPlan)
    execution = repo.load_artifact(
        s, project_id, repo.ArtifactKind.EXECUTION, TransformationExecution
    )
    model = repo.load_artifact(s, project_id, repo.ArtifactKind.MODEL, SemanticModel)
    report = repo.load_artifact(s, project_id, repo.ArtifactKind.REPORT, ReportSpec)
    validation = repo.load_artifact(
        s, project_id, repo.ArtifactKind.VALIDATION, ValidationReport
    )

    recommendations: list[ImprovementRecommendation] = []
    recommendations += _missing_kpis(project_id, profile, requirements, model)
    recommendations += _modelling(project_id, model, profile)
    recommendations += _transformations(project_id, plan, execution)
    recommendations += _measure_quality(project_id, model, requirements)
    recommendations += _performance(project_id, model, execution)
    recommendations += _consistency(project_id, profile, requirements)
    recommendations += _insights(project_id, profile, model)
    recommendations += _visualisation(project_id, report, model, requirements)
    recommendations += _from_validation(project_id, validation)
    recommendations += _process(project_id, profile, requirements)

    order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2,
             Severity.LOW: 3, Severity.INFO: 4}
    recommendations.sort(key=lambda r: (order[r.severity], r.area.value))

    out = ImprovementReport(
        project_id=project_id, recommendations=recommendations,
        summary=_summary(recommendations),
    )
    return {
        "report": out,
        "notes": [
            f"{len(recommendations)} recommendation(s) across "
            f"{len({r.area for r in recommendations})} area(s). None has been applied: "
            f"each needs approval, and approving one re-runs the responsible agent so "
            f"the change stays auditable."
        ],
    }


def _summary(recommendations: list[ImprovementRecommendation]) -> str:
    if not recommendations:
        return "No improvements were identified from the current artifacts."
    by_area: dict[str, int] = {}
    for r in recommendations:
        by_area[r.area.value] = by_area.get(r.area.value, 0) + 1
    critical = [r for r in recommendations if r.severity is Severity.CRITICAL]
    parts = [
        f"{len(recommendations)} recommendation(s): "
        + ", ".join(f"{count} {area.replace('_', ' ')}"
                    for area, count in sorted(by_area.items()))
        + "."
    ]
    if critical:
        parts.append(
            f"{len(critical)} are critical and would change reported numbers: "
            + "; ".join(r.title for r in critical[:3]) + "."
        )
    return " ".join(parts)


# --------------------------------------------------------------------------
def _missing_kpis(
    project_id: str, profile: SourceDataProfile | None,
    requirements: RequirementsSpec | None, model: SemanticModel | None,
) -> list[ImprovementRecommendation]:
    """KPIs the data supports but nobody asked for."""
    if model is None:
        return []
    out: list[ImprovementRecommendation] = []
    roles = {
        kpi_catalog.role_for_column(c.name)
        for t in model.tables for c in t.columns
        if not c.is_hidden
    }
    existing = {m.name.lower() for m in model.measures}

    candidates = [
        ("cost_per_hour", {"cost", "hours"}, "Cost per Hour",
         "Both cost and labour hours are modelled, so an effective rate is "
         "computable without any new data.",
         "An effective hourly rate exposes projects where labour is expensive "
         "relative to output, which neither total alone shows.",
         ["Only meaningful where both facts are filtered to the same project and "
          "period; at a mixed grain it is misleading.",
          "It mixes two fact tables, so a reader may mistake it for a rate stored in "
          "the data."]),
        ("project_count", {"project_key"}, "Project Count",
         "A distinct count of projects is the denominator most per-project figures "
         "need, and making it explicit stops each measure recomputing it.",
         "One agreed definition of 'how many projects', reusable by every ratio.",
         ["Distinct counts do not sum across slices, so a reader adding them will get "
          "the wrong answer."]),
        ("pct_of_total", {"cost", "category"}, "Cost % of Total",
         "Cost and a category dimension are both present, so each category's share "
         "is computable.",
         "Shows concentration -- whether one category dominates spend -- which an "
         "absolute total hides.",
         ["A ratio to an outer total behaves unexpectedly when the reader also applies "
          "a page filter."]),
    ]
    for key, needed, name, rationale, benefit, trade_offs in candidates:
        if name.lower() in existing or not needed <= roles:
            continue
        out.append(_rec(
            project_id, ImprovementArea.MISSING_KPI, f"Add a '{name}' measure",
            rationale=rationale,
            evidence=[
                f"the model carries every role this needs: {', '.join(sorted(needed))}",
                f"no measure named '{name}' exists",
            ],
            benefit=benefit, trade_offs=list(trade_offs), effort="low",
            severity=Severity.LOW,
            change={"action": "add_kpi", "kpi_name": name, "shape": key},
        ))
    return out


def _modelling(
    project_id: str, model: SemanticModel | None, profile: SourceDataProfile | None
) -> list[ImprovementRecommendation]:
    if model is None:
        return []
    out: list[ImprovementRecommendation] = []

    # high-cardinality text on a fact table
    for table in model.fact_tables:
        wide = [
            c.name for c in table.columns
            if not c.is_hidden and not c.is_key
            and c.data_type.value in {"string", "identifier"}
            and kpi_catalog.role_for_column(c.name) in {"vendor", "invoice", "name", ""}
        ]
        if len(wide) >= 2 and table.row_count > 100:
            out.append(_rec(
                project_id, ImprovementArea.MODELLING,
                f"Move the descriptive columns off '{table.name}' into a dimension",
                rationale=f"'{table.name}' carries {len(wide)} text column(s) that "
                          f"describe an entity rather than the event. On a fact table "
                          f"each value is stored once per row.",
                evidence=[
                    f"{table.name} has {table.row_count:,} rows",
                    f"text columns on the fact: {', '.join(wide[:6])}",
                ],
                benefit="Smaller model, faster slicers, and the attributes become "
                        "reusable across both fact tables instead of belonging to one.",
                trade_offs=[
                    "Adds a table and a relationship, so the model is slightly more "
                    "complex to read.",
                    "A reader used to finding the vendor on the cost table will have to "
                    "look in the new dimension.",
                ],
                effort="medium", severity=Severity.LOW,
                objects=[f"{table.name}.{c}" for c in wide[:6]],
                change={"action": "extract_dimension", "table": table.name,
                        "columns": wide},
            ))

    # a dimension carrying measures, which is a real double-count hazard
    for table in model.tables:
        if table.role is not TableRole.DIMENSION:
            continue
        numeric = [
            c.name for c in table.columns
            if c.data_type.is_numeric and not c.is_hidden and not c.is_key
        ]
        if numeric and model.fact_tables:
            out.append(_rec(
                project_id, ImprovementArea.MODELLING,
                f"Consider a separate budget fact table instead of holding "
                f"{', '.join(numeric[:2])} on '{table.name}'",
                rationale=f"'{table.name}' is a dimension that also stores "
                          f"{len(numeric)} numeric column(s). Any measure over them is "
                          f"correct at the dimension's own grain and silently repeats "
                          f"below it.",
                evidence=[
                    f"{table.name} is related to "
                    f"{len([r for r in model.relationships if r.to_table == table.name])}"
                    f" fact table(s)",
                    f"numeric columns: {', '.join(numeric)}",
                ],
                benefit="A budget fact at its own grain (project x period) lets budget "
                        "be sliced by date as well as project, which the current shape "
                        "cannot do correctly.",
                trade_offs=[
                    "Requires budget data at a finer grain than the register currently "
                    "holds; if only an annual figure exists, splitting it would invent "
                    "precision.",
                    "Two fact tables for cost and budget need a conformed date "
                    "dimension to compare, which exists but is one more thing to "
                    "maintain.",
                ],
                effort="high", severity=Severity.MEDIUM,
                objects=[f"{table.name}.{c}" for c in numeric],
                change={"action": "split_budget_fact", "table": table.name,
                        "columns": numeric},
            ))

    for issue in model.issues:
        if issue.code == "model.ambiguous_path":
            out.append(_rec(
                project_id, ImprovementArea.MODELLING,
                f"Resolve the ambiguous filter path at {issue.location}",
                rationale=issue.message,
                evidence=[str(issue.evidence)[:200]],
                benefit="Power BI refuses ambiguous active paths, so the model will not "
                        "load until this is settled.",
                trade_offs=["Deactivating a relationship means any measure that needs "
                            "that path must use USERELATIONSHIP explicitly."],
                effort="low", severity=Severity.HIGH,
                objects=[issue.location],
                change={"action": "deactivate_relationship", "location": issue.location},
            ))
    return out


def _transformations(
    project_id: str, plan: TransformationPlan | None,
    execution: TransformationExecution | None,
) -> list[ImprovementRecommendation]:
    if plan is None:
        return []
    out: list[ImprovementRecommendation] = []

    # steps that changed nothing
    if execution:
        no_ops = [
            d for d in execution.row_count_deltas
            if d.op.value in {"deduplicate", "remove_empty_rows", "filter_rows"}
            and d.delta == 0
        ]
        if no_ops:
            out.append(_rec(
                project_id, ImprovementArea.TRANSFORMATION,
                f"Remove {len(no_ops)} transformation step(s) that changed nothing",
                rationale="These steps ran and removed no rows, so they add refresh time "
                          "and reading effort for no effect on this data.",
                evidence=[f"{d.op.value} on {d.table}: {d.rows_before} -> {d.rows_after}"
                          for d in no_ops[:5]],
                benefit="A shorter, clearer Power Query that a developer can read.",
                trade_offs=[
                    "The step may be defensive: it removes nothing today but would "
                    "catch duplicates in a future refresh. Removing it trades safety "
                    "for brevity.",
                ],
                effort="low", severity=Severity.LOW,
                change={"action": "remove_steps",
                        "step_ids": [d.step_id for d in no_ops]},
            ))

    risky = [j for j in plan.join_proposals if j.fan_out_risk]
    if risky:
        out.append(_rec(
            project_id, ImprovementArea.JOIN_STRATEGY,
            f"Replace {len(risky)} fan-out-prone join(s) with a bridge dimension",
            rationale="The parent side of these joins is not unique, so materialising "
                      "them would duplicate rows and inflate every additive measure.",
            evidence=[
                f"{j.left_table}.{j.left_keys[0]} -> {j.right_table}.{j.right_keys[0]}: "
                f"parent not unique, {j.match_share:.1%} matched"
                for j in risky[:4]
            ],
            benefit="Correct totals. A bridge table of the distinct keys makes the "
                    "relationship many-to-one on both sides.",
            trade_offs=[
                "A bridge adds a table and a hop, which makes some measures need "
                "CROSSFILTER to propagate the way a reader expects.",
            ],
            effort="medium", severity=Severity.HIGH,
            change={"action": "add_bridge", "joins": [j.id for j in risky]},
        ))

    unapproved = plan.steps_awaiting_approval
    if unapproved:
        out.append(_rec(
            project_id, ImprovementArea.TRANSFORMATION,
            f"Decide the {len(unapproved)} transformation step(s) still awaiting approval",
            rationale="These steps were not applied, so the prepared data still has the "
                      "problems they address.",
            evidence=[f"{s.title} ({s.risk.value})" for s in unapproved[:5]],
            benefit="The datasets would reflect the full plan rather than part of it.",
            trade_offs=["Each is gated because it changes values or drops rows; "
                        "approving without reviewing the preview defeats the gate."],
            effort="low", severity=Severity.MEDIUM,
            change={"action": "review_approvals",
                    "step_ids": [s.id for s in unapproved]},
        ))
    return out


def _measure_quality(
    project_id: str, model: SemanticModel | None, requirements: RequirementsSpec | None
) -> list[ImprovementRecommendation]:
    if model is None:
        return []
    out: list[ImprovementRecommendation] = []

    # a plain average of a rate
    for measure in model.measures:
        if "AVERAGE(" not in measure.expression:
            continue
        if "weighted" in measure.aggregation_behaviour.lower():
            continue
        out.append(_rec(
            project_id, ImprovementArea.MEASURE_QUALITY,
            f"Weight '{measure.name}' instead of averaging it unweighted",
            rationale=f"'{measure.name}' is a plain AVERAGE of a rate, so a $10k "
                      f"project counts as much as a $10m one. For a portfolio view "
                      f"that is usually not what the reader means.",
            evidence=[
                f"expression: {measure.expression}",
                measure.aggregation_behaviour,
            ],
            benefit="A budget-weighted average reflects portfolio reality: the big "
                    "projects move the number.",
            trade_offs=[
                "A weighted figure is harder to explain and cannot be reconciled by "
                "eye against the project table.",
                "It needs a weight everyone agrees on -- budget and cost give "
                "different answers.",
            ],
            effort="low", severity=Severity.MEDIUM,
            objects=[measure.name],
            change={"action": "weight_measure", "measure": measure.name,
                    "weight_role": "budget"},
        ))

    without_format = [m.name for m in model.measures if not m.format_string]
    if without_format:
        out.append(_rec(
            project_id, ImprovementArea.MEASURE_QUALITY,
            f"Set a format string on {len(without_format)} measure(s)",
            rationale="Without a format string Power BI shows a raw number, so currency "
                      "and percentage measures read as plain decimals.",
            evidence=[f"unformatted: {', '.join(without_format[:6])}"],
            benefit="Figures read correctly everywhere they appear, including tooltips "
                    "and exported data.",
            trade_offs=["None of substance; a format string can be overridden per "
                        "visual if a page needs different precision."],
            effort="low", severity=Severity.LOW,
            objects=without_format,
            change={"action": "set_format_strings", "measures": without_format},
        ))

    if requirements:
        undefined = [k.name for k in requirements.kpis if not k.definition]
        if undefined:
            out.append(_rec(
                project_id, ImprovementArea.MISSING_KPI,
                f"Define {len(undefined)} requested KPI(s) so a measure can be built",
                rationale="These were requested but have no agreed definition, so no "
                          "measure was generated. That is the right outcome -- a formula "
                          "from a guessed definition produces a confidently wrong "
                          "number -- but the KPI is still missing.",
                evidence=[f"undefined: {', '.join(undefined)}"],
                benefit="The stakeholder gets the KPI they asked for.",
                trade_offs=["Needs a decision from whoever owns the definition; it "
                            "cannot be resolved from the data."],
                effort="low", severity=Severity.HIGH,
                objects=undefined,
                change={"action": "answer_questions", "kpis": undefined},
            ))
    return out


def _performance(
    project_id: str, model: SemanticModel | None,
    execution: TransformationExecution | None,
) -> list[ImprovementRecommendation]:
    if model is None:
        return []
    out: list[ImprovementRecommendation] = []

    for table in model.tables:
        high_cardinality = [
            c.name for c in table.columns
            if c.name.endswith(("_original", "_unparsed", "_source_format"))
            and not c.is_hidden
        ]
        if high_cardinality:
            out.append(_rec(
                project_id, ImprovementArea.PERFORMANCE,
                f"Drop the audit columns from '{table.name}' before publishing",
                rationale="Audit columns exist so a reviewer can see what the "
                          "preparation changed. Once the transformation is accepted they "
                          "are dead weight in the model.",
                evidence=[f"audit columns on {table.name}: "
                          f"{', '.join(high_cardinality)}"],
                benefit="A smaller model and a shorter field list.",
                trade_offs=[
                    "Removing them loses the ability to show a reviewer the original "
                    "value next to the cleaned one, which is exactly what the "
                    "stakeholder asked for in the sample project ('tell me which rows "
                    "you changed'). Keep them until the data owner has signed off.",
                ],
                effort="low", severity=Severity.LOW,
                objects=[f"{table.name}.{c}" for c in high_cardinality],
                change={"action": "hide_columns", "table": table.name,
                        "columns": high_cardinality},
            ))
            break

    date_table = next((t for t in model.date_tables), None)
    if date_table and date_table.row_count > 3000:
        out.append(_rec(
            project_id, ImprovementArea.PERFORMANCE,
            "Narrow the date dimension to the reporting period",
            rationale=f"The calendar covers {date_table.row_count:,} days, which is "
                      f"wider than the data needs.",
            evidence=[f"{date_table.name} has {date_table.row_count:,} rows"],
            benefit="A smaller model and fewer empty periods on a trend axis.",
            trade_offs=[
                "A narrower calendar breaks year-on-year comparison against earlier "
                "data, and must be widened again when new data arrives.",
            ],
            effort="low", severity=Severity.LOW,
            objects=[date_table.name],
            change={"action": "narrow_date_table", "table": date_table.name},
        ))
    return out


def _consistency(
    project_id: str, profile: SourceDataProfile | None,
    requirements: RequirementsSpec | None,
) -> list[ImprovementRecommendation]:
    if profile is None:
        return []
    out: list[ImprovementRecommendation] = []
    conflicts = [
        i for i in profile.cross_table_issues
        if i.code == "keys.conflicting_category_values"
    ]
    for issue in conflicts:
        out.append(_rec(
            project_id, ImprovementArea.METRIC_CONSISTENCY,
            f"Agree one vocabulary for {issue.location}",
            rationale=issue.message,
            evidence=[str(issue.evidence)[:220]],
            benefit="One slicer that filters every table consistently, instead of two "
                    "status columns a reader has to reconcile.",
            trade_offs=[
                "Mapping one vocabulary onto the other needs a decision from the system "
                "owners; choosing wrongly silently mislabels rows.",
            ],
            effort="medium", severity=Severity.MEDIUM,
            objects=[issue.location],
            change={"action": "map_vocabulary", "location": issue.location},
        ))

    multi_currency = [
        f"{t.table_name}.{c.name}"
        for t in profile.tables for c in t.columns
        if len(c.detected_currency_symbols) > 1
    ]
    if multi_currency:
        out.append(_rec(
            project_id, ImprovementArea.DATA_COLLECTION,
            "Capture currency and amount as separate fields at source",
            rationale="At least one amount column contains more than one currency. The "
                      "pipeline detects and excludes the odd ones, but the underlying "
                      "export is ambiguous.",
            evidence=[f"mixed-currency columns: {', '.join(multi_currency)}"],
            benefit="Every row becomes convertible, so nothing has to be excluded from "
                    "a reporting-currency total.",
            trade_offs=[
                "Requires a change in the source finance system, which is slower than "
                "handling it in the pipeline.",
                "Needs an agreed FX rate and rate date -- itself an open question in "
                "this project.",
            ],
            effort="high", severity=Severity.HIGH,
            objects=multi_currency,
            change={"action": "source_change", "columns": multi_currency},
        ))
    return out


def _insights(
    project_id: str, profile: SourceDataProfile | None, model: SemanticModel | None
) -> list[ImprovementRecommendation]:
    if model is None:
        return []
    out: list[ImprovementRecommendation] = []
    roles = {
        kpi_catalog.role_for_column(c.name)
        for t in model.tables for c in t.columns if not c.is_hidden
    }
    if {"cost", "date"} <= roles:
        out.append(_rec(
            project_id, ImprovementArea.ADDITIONAL_INSIGHT,
            "Add a burn-rate measure and a forecast-to-completion",
            rationale="Cost and a date dimension are both present, so the rate of spend "
                      "and a straight-line projection to the planned finish are "
                      "computable from data already in the model.",
            evidence=[
                "the model has cost at transaction grain and a contiguous calendar",
                "a planned finish date is held on the project dimension",
            ],
            benefit="Answers 'will this project finish within budget', which the current "
                    "report cannot -- it only reports what has been spent.",
            quantified="",
            trade_offs=[
                "A straight-line projection is wrong for projects with front- or "
                "back-loaded spend, and presenting it as a forecast invites more "
                "confidence than it deserves.",
                "Needs a stated convention for projects with no recent activity.",
            ],
            effort="medium", severity=Severity.LOW,
            change={"action": "add_kpi", "kpi_name": "Burn Rate", "shape": ""},
        ))
    if {"hours", "cost"} <= roles:
        out.append(_rec(
            project_id, ImprovementArea.ADDITIONAL_INSIGHT,
            "Compare labour hours against the labour cost category",
            rationale="Hours are recorded separately from the ledger's labour cost "
                      "category, so the two can be reconciled against each other.",
            evidence=["timesheet hours and a labour cost category both exist"],
            benefit="Surfaces timesheets with no matching cost, and labour cost with no "
                    "timesheet -- both are data-collection gaps worth knowing about.",
            trade_offs=[
                "The two are at different grains, so the comparison only holds at "
                "project and month level.",
                "A difference may be a timing lag rather than an error, which needs "
                "explaining on the report.",
            ],
            effort="medium", severity=Severity.LOW,
            change={"action": "add_reconciliation_page"},
        ))
    return out


def _visualisation(
    project_id: str, report: ReportSpec | None, model: SemanticModel | None,
    requirements: RequirementsSpec | None,
) -> list[ImprovementRecommendation]:
    if report is None:
        return []
    out: list[ImprovementRecommendation] = []
    if report.unmapped_kpis:
        out.append(_rec(
            project_id, ImprovementArea.VISUALISATION,
            f"Give {len(report.unmapped_kpis)} KPI(s) a visual",
            rationale="These KPIs were requested but no visual reports them, so a reader "
                      "cannot see them without building something themselves.",
            evidence=[f"unmapped: {', '.join(report.unmapped_kpis)}"],
            benefit="The report answers every question it was asked to.",
            trade_offs=["Adding visuals to an already-full page reduces its clarity; a "
                        "second page may be better than a crowded first one."],
            effort="low", severity=Severity.MEDIUM,
            objects=report.unmapped_kpis,
            change={"action": "add_visuals", "kpis": report.unmapped_kpis},
        ))

    has_variance = any(
        "variance" in m.name.lower() for m in (model.measures if model else [])
    )
    has_conditional = any(
        v.visual_type.value in {"kpi", "gauge"}
        for p in report.pages for v in p.visuals
    )
    if has_variance and not has_conditional:
        out.append(_rec(
            project_id, ImprovementArea.VISUALISATION,
            "Use a KPI visual with conditional formatting for budget variance",
            rationale="Variance has a direction that matters -- over or under budget -- "
                      "and a plain card gives the number without the judgement.",
            evidence=["a variance measure exists",
                      "no KPI or gauge visual is used on any page"],
            benefit="A reader sees at a glance which projects are in trouble, instead of "
                    "comparing signs across a table.",
            trade_offs=[
                "Conditional colour needs an agreed threshold; picking one arbitrarily "
                "makes the report assert a tolerance nobody set.",
                "The sign convention must be confirmed first, or the colours will be "
                "exactly inverted.",
            ],
            effort="low", severity=Severity.LOW,
            change={"action": "add_kpi_visual", "measure": "Budget Variance"},
        ))
    return out


def _from_validation(
    project_id: str, validation: ValidationReport | None
) -> list[ImprovementRecommendation]:
    """Turn QA's unsupported checks into an honest recommendation."""
    if validation is None:
        return []
    unsupported = [
        r for r in validation.results if r.status is TestStatus.NOT_SUPPORTED
    ]
    if not unsupported:
        return []
    targets = sorted({r.target or r.test_name for r in unsupported})
    return [_rec(
        project_id, ImprovementArea.MEASURE_QUALITY,
        f"Verify {len(targets)} measure(s) in Power BI Desktop that could not be "
        f"evaluated here",
        rationale="These measures use DAX outside the subset this environment can "
                  "execute. Their syntax and references were checked, but their values "
                  "were not, so a numeric error in them would not have been caught.",
        evidence=[f"not evaluated: {', '.join(targets[:8])}"]
        + [f"reason: {unsupported[0].skipped_reason[:160]}"],
        benefit="Closes the one real gap in the validation: these are the only measures "
                "whose numbers are unverified.",
        trade_offs=["Requires a Windows machine with Power BI Desktop, which this "
                    "environment is not."],
        effort="low", severity=Severity.MEDIUM,
        objects=targets,
        change={"action": "manual_verification", "measures": targets},
    )]


def _process(
    project_id: str, profile: SourceDataProfile | None,
    requirements: RequirementsSpec | None,
) -> list[ImprovementRecommendation]:
    if profile is None:
        return []
    out: list[ImprovementRecommendation] = []

    ambiguous = [
        f"{t.table_name}.{c.name}"
        for t in profile.tables for c in t.columns
        for i in c.issues
        if i.code in {"column.conflicting_date_orders", "column.ambiguous_date_order"}
    ]
    if ambiguous:
        out.append(_rec(
            project_id, ImprovementArea.BUSINESS_PROCESS,
            "Export dates in ISO format (YYYY-MM-DD) from the source system",
            rationale="At least one date column mixes day-first and month-first values, "
                      "which cannot be resolved from the data for days 1-12. The "
                      "pipeline parses each detected format, but genuinely ambiguous "
                      "values still need a human decision.",
            evidence=[f"ambiguous or conflicting date columns: "
                      f"{', '.join(sorted(set(ambiguous))[:5])}"],
            benefit="Removes a whole class of silent error: a mis-parsed date moves a "
                    "transaction into the wrong month and the total still looks right.",
            trade_offs=[
                "Requires a change to the export configuration in the source system, "
                "which may be owned by another team.",
            ],
            effort="low", severity=Severity.HIGH,
            objects=sorted(set(ambiguous)),
            change={"action": "source_change", "columns": sorted(set(ambiguous))},
        ))

    orphans = [
        i for i in profile.cross_table_issues
        if i.code == "keys.unmatched_identifiers"
    ]
    if orphans:
        out.append(_rec(
            project_id, ImprovementArea.BUSINESS_PROCESS,
            "Reconcile project codes between the ledger and the register",
            rationale="Codes appear in the cost data that the project register does not "
                      "contain. The pipeline keeps those rows under an 'UNASSIGNED' "
                      "member so totals still reconcile, but the underlying mismatch is "
                      "a process gap.",
            evidence=[i.message for i in orphans[:3]],
            benefit="Every cost row attributable to a real project, so portfolio "
                    "breakdowns account for all spend rather than most of it.",
            trade_offs=[
                "Needs the PMO and Finance to agree who owns project creation; a "
                "pipeline fix cannot settle that.",
            ],
            effort="medium", severity=Severity.HIGH,
            objects=[i.location for i in orphans[:5]],
            change={"action": "process_review", "locations": [i.location for i in orphans]},
        ))
    return out
