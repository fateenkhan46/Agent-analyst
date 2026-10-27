"""Generating DAX measures from approved KPI requirements.

Every measure is built from a template that is **grounded in the model**: the
generator resolves the actual table and column for each semantic role before
it writes a single character of DAX. If it cannot resolve them, it emits no
measure and records why -- a measure referencing a column that does not exist
is worse than no measure, because it looks like progress.

Each generated measure then goes through ``validate_measure`` before it is
returned. One that fails is moved to ``rejected`` with the reason, so the
failure is visible in the audit trail rather than silently dropped.

Templates encode the DAX fundamentals the brief asks for:

* ``DIVIDE`` for every ratio, so a zero or missing denominator returns BLANK
  rather than infinity -- which is exactly what the sample meeting asks for.
* explicit measures, never calculated columns, for anything additive.
* time intelligence only where the model actually has a date dimension.
* ``DISTINCTCOUNT`` over the fact's key for "per project" denominators, not a
  row count of the dimension, so a project with no activity does not drag the
  average down.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...schemas.common import Confidence, Origin, Provenance
from ...schemas.requirements import AggregationKind, KpiRequirement, RequirementsSpec
from ...schemas.semantic import (
    MeasureDefinition, MeasureValidationCase, SemanticModel, TableDefinition, TableRole,
)
from .. import kpi_catalog


@dataclass
class Grounding:
    """Where a semantic role actually lives in the model."""

    table: str = ""
    column: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.table and self.column)

    @property
    def reference(self) -> str:
        return f"{_table_ref(self.table)}[{self.column}]"


def _table_ref(name: str) -> str:
    """Quote a table name if it needs it."""
    return name if name.replace("_", "").isalnum() else f"'{name}'"


def _ground(model: SemanticModel, role: str, *, prefer_fact: bool = True) -> Grounding:
    """Find the table and column that carry a semantic role.

    Preference order matters. For a measure role we want the *fact* table, so
    ``cost`` resolves to FactProjectCosts[cost_amount] rather than to a
    same-named column on a dimension. For a key role used as a denominator we
    want the fact side too, because that is what "projects that have cost"
    means.
    """
    candidates: list[tuple[TableDefinition, str]] = []
    for table in model.tables:
        for column in table.columns:
            if column.is_hidden:
                continue
            resolved = kpi_catalog.role_for_column(column.name)
            if resolved == role:
                candidates.append((table, column.name))
    if not candidates:
        return Grounding()

    def rank(entry: tuple[TableDefinition, str]) -> tuple[int, int]:
        table, _ = entry
        is_fact = table.role is TableRole.FACT
        preferred = is_fact if prefer_fact else not is_fact
        return (0 if preferred else 1, -table.row_count)

    candidates.sort(key=rank)
    table, column = candidates[0]
    return Grounding(table=table.name, column=column)


def _currency_guard(model: SemanticModel, grounding: Grounding) -> tuple[str, str]:
    """A filter restricting an amount to the reporting currency, if one exists.

    The preparation step adds ``<column>_is_reporting_currency`` whenever it
    finds more than one currency in an amount column. Summing across
    currencies produces a number that is not money in any unit, so wherever
    that flag exists the measure filters on it and says so. This is also an
    explicit stakeholder requirement in the sample project: "don't just add it
    to the dollars".

    Returns (filter_clause, note) -- both empty when the column is
    single-currency and no guard is needed.
    """
    if not grounding.resolved:
        return "", ""
    table = model.table(grounding.table)
    if table is None:
        return "", ""
    flag_name = f"{grounding.column}_is_reporting_currency"
    flag = table.column(flag_name)
    if flag is None:
        return "", ""
    reference = f"{_table_ref(table.name)}[{flag.name}]"
    return (
        f"{reference} = TRUE",
        f"Restricted to the reporting currency: '{grounding.table}.{grounding.column}' "
        f"contains more than one currency, and adding unlike currencies together "
        f"produces a meaningless total. Rows in another currency are excluded and "
        f"reported separately until a conversion rate is supplied.",
    )


def _wrap_currency(expression: str, guard: str) -> str:
    return f"CALCULATE({expression}, {guard})" if guard else expression


def _date_grounding(model: SemanticModel) -> Grounding:
    for table in model.date_tables:
        for column in table.columns:
            if column.data_type.is_temporal:
                return Grounding(table=table.name, column=column.name)
    return Grounding()


def _measure_name_for(kpi: KpiRequirement) -> str:
    return kpi.name.strip()


def _explanation(kpi: KpiRequirement, expression: str, grounding: dict[str, Grounding]) -> str:
    parts = [kpi.definition.strip() or f"Calculates {kpi.name}."]
    refs = [g.reference for g in grounding.values() if g.resolved]
    if refs:
        parts.append("Reads " + ", ".join(sorted(set(refs))) + ".")
    return " ".join(parts)


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------
def _simple_aggregation(
    kpi: KpiRequirement, model: SemanticModel
) -> tuple[str, dict[str, Grounding], str, str] | None:
    role = kpi.numerator_hint or {
        AggregationKind.SUM: "cost", AggregationKind.AVERAGE: "cost",
    }.get(kpi.aggregation, "")
    if not role:
        return None
    grounding = _ground(model, role)
    if not grounding.resolved:
        return None
    function = {
        AggregationKind.SUM: "SUM", AggregationKind.AVERAGE: "AVERAGE",
        AggregationKind.MIN: "MIN", AggregationKind.MAX: "MAX",
        AggregationKind.COUNT: "COUNT",
        AggregationKind.DISTINCT_COUNT: "DISTINCTCOUNT",
    }.get(kpi.aggregation, "SUM")
    guard, guard_note = _currency_guard(model, grounding)
    expression = _wrap_currency(f"{function}({grounding.reference})", guard)
    behaviour = (
        "additive: sums correctly across every dimension"
        if function == "SUM" else
        "non-additive: an average of averages is not the average of the whole"
        if function == "AVERAGE" else "aggregates at each level independently"
    )
    if guard_note:
        behaviour = f"{behaviour.rstrip('. ')}. {guard_note}"
    return expression, {"value": grounding}, grounding.table, behaviour


def _budget_measure(
    kpi: KpiRequirement, model: SemanticModel
) -> tuple[str, dict[str, Grounding], str, str] | None:
    """Budget lives on the project dimension, so it is summed there.

    Summing it over a joined fact table would repeat each project's budget once
    per cost row. Keeping the aggregation on the dimension is what prevents
    that, and the aggregation behaviour says so explicitly.
    """
    grounding = _ground(model, "budget", prefer_fact=False)
    if not grounding.resolved:
        return None
    guard, guard_note = _currency_guard(model, grounding)
    return (
        _wrap_currency(f"SUM({grounding.reference})", guard),
        {"value": grounding},
        grounding.table,
        "additive across projects only. Budget is stored once per project, so slicing "
        "by cost category or by date repeats the same value -- the measure is correct "
        "at project grain and above, and misleading below it."
        + (f" {guard_note}" if guard_note else ""),
    )


def _variance(
    kpi: KpiRequirement, model: SemanticModel, measures: dict[str, str]
) -> tuple[str, dict[str, Grounding], str, str] | None:
    budget = measures.get("total budget")
    cost = measures.get("total cost")
    if not budget or not cost:
        return None
    return (
        f"[{budget}] - [{cost}]",
        {},
        "",
        "non-additive at a level finer than the project, because its budget component is.",
    )


def _variance_pct(
    kpi: KpiRequirement, model: SemanticModel, measures: dict[str, str]
) -> tuple[str, dict[str, Grounding], str, str] | None:
    variance = measures.get("budget variance")
    budget = measures.get("total budget")
    if not variance or not budget:
        return None
    # DIVIDE, not "/", so a zero or missing budget gives BLANK rather than
    # infinity -- which the stakeholder in the sample meeting asks for by name.
    return (
        f"DIVIDE([{variance}], [{budget}])",
        {},
        "",
        "non-additive (a ratio). Returns BLANK when budget is zero or missing.",
    )


def _average_per_entity(
    kpi: KpiRequirement, model: SemanticModel, measures: dict[str, str]
) -> tuple[str, dict[str, Grounding], str, str] | None:
    numerator_measure = measures.get("total cost")
    key = _ground(model, "project_key")
    if not key.resolved:
        return None
    numerator = (
        f"[{numerator_measure}]" if numerator_measure
        else (lambda g: f"SUM({g.reference})" if g.resolved else "")(
            _ground(model, "cost")
        )
    )
    if not numerator:
        return None
    # DISTINCTCOUNT over the FACT's key, so the denominator counts projects
    # that actually have cost -- a register entry with no activity must not
    # drag the average down.
    return (
        f"DIVIDE({numerator}, DISTINCTCOUNT({key.reference}))",
        {"key": key},
        key.table,
        "non-additive (a ratio). The denominator counts distinct projects present in "
        "the fact table, not rows of the register, so a project with no activity is "
        "excluded.",
    )


def _ratio_of_roles(
    kpi: KpiRequirement, model: SemanticModel
) -> tuple[str, dict[str, Grounding], str, str] | None:
    numerator = _ground(model, kpi.numerator_hint)
    denominator = _ground(model, kpi.denominator_hint)
    if not numerator.resolved or not denominator.resolved:
        return None
    return (
        f"DIVIDE(SUM({numerator.reference}), SUM({denominator.reference}))",
        {"numerator": numerator, "denominator": denominator},
        numerator.table,
        "non-additive (a ratio); meaningful only where both components share the same "
        "filter context.",
    )


def _average_of_rate(
    kpi: KpiRequirement, model: SemanticModel
) -> tuple[str, dict[str, Grounding], str, str] | None:
    grounding = _ground(model, kpi.numerator_hint or "completion", prefer_fact=False)
    if not grounding.resolved:
        return None
    return (
        f"AVERAGE({grounding.reference})",
        {"value": grounding},
        grounding.table,
        "non-additive (an average of a rate). Weights every project equally regardless "
        "of size; a budget-weighted average is usually more meaningful.",
    )


def _period_change(
    kpi: KpiRequirement, model: SemanticModel, measures: dict[str, str]
) -> tuple[str, dict[str, Grounding], str, str] | None:
    base = measures.get("total cost")
    date = _date_grounding(model)
    if not base or not date.resolved:
        return None
    expression = (
        f"VAR Current = [{base}]\n"
        f"VAR Prior = CALCULATE([{base}], PREVIOUSMONTH({date.reference}))\n"
        f"RETURN IF(ISBLANK(Prior), BLANK(), Current - Prior)"
    )
    return (
        expression, {"date": date}, "",
        "non-additive (a period-over-period difference). Returns BLANK for the first "
        "month in context rather than reporting the full value as growth.",
    )


def _running_total(
    kpi: KpiRequirement, model: SemanticModel, measures: dict[str, str]
) -> tuple[str, dict[str, Grounding], str, str] | None:
    base = measures.get("total cost")
    date = _date_grounding(model)
    if not base or not date.resolved:
        return None
    return (
        f"TOTALYTD([{base}], {date.reference})",
        {"date": date}, "",
        "semi-additive: cumulative over time, so it must not be summed across periods.",
    )


def _distinct_count(
    kpi: KpiRequirement, model: SemanticModel
) -> tuple[str, dict[str, Grounding], str, str] | None:
    grounding = _ground(model, kpi.numerator_hint or "project_key")
    if not grounding.resolved:
        return None
    return (
        f"DISTINCTCOUNT({grounding.reference})",
        {"key": grounding}, grounding.table,
        "non-additive: distinct counts do not sum across slices.",
    )


def _percent_of_total(
    kpi: KpiRequirement, model: SemanticModel, measures: dict[str, str]
) -> tuple[str, dict[str, Grounding], str, str] | None:
    base = measures.get("total cost")
    if not base:
        return None
    fact = next((t for t in model.fact_tables), None)
    if fact is None:
        return None
    return (
        f"DIVIDE([{base}], CALCULATE([{base}], REMOVEFILTERS({_table_ref(fact.name)})))",
        {}, fact.name,
        "non-additive (a ratio to an outer total). REMOVEFILTERS on the fact table gives "
        "the grand total; ALL would also discard page-level filters the user set.",
    )


# --------------------------------------------------------------------------
# Validation cases
# --------------------------------------------------------------------------
def _validation_cases(
    kpi: KpiRequirement, expression: str, model: SemanticModel,
    grounding: dict[str, Grounding],
) -> list[MeasureValidationCase]:
    """Concrete expectations QA can check, derived independently of the DAX."""
    cases: list[MeasureValidationCase] = []
    name = _measure_name_for(kpi)

    cases.append(MeasureValidationCase(
        name=f"{name} returns a value at the grand total",
        expected_source="A measure that returns BLANK with no filter usually means its "
                        "source column is empty or the aggregation resolved nothing.",
        comparison="not_null",
    ))

    if kpi.aggregation is AggregationKind.SUM and "value" in grounding:
        g = grounding["value"]
        cases.append(MeasureValidationCase(
            name=f"{name} equals the column total computed directly",
            expected_source=f"Independently computed as SUM of {g.table}.{g.column} "
                            f"straight from the prepared dataset, bypassing the DAX.",
            comparison="equals", tolerance=0.01,
        ))
        cases.append(MeasureValidationCase(
            name=f"{name} is non-negative",
            expected_source="Costs, budgets and hours should not be negative in total; a "
                            "negative grand total indicates sign errors or credit notes "
                            "that need separate treatment.",
            comparison="sign", lower=0.0,
        ))

    if kpi.unit == "%" or kpi.format_string.endswith("%"):
        cases.append(MeasureValidationCase(
            name=f"{name} falls in a plausible percentage range",
            expected_source="A ratio outside -10..10 (i.e. -1000%..1000%) usually means "
                            "the denominator is wrong or near zero.",
            comparison="in_range", lower=-10.0, upper=10.0,
        ))

    # a sliced case, so filter behaviour is exercised rather than assumed
    dimension = next(
        (
            t for t in model.tables
            if t.role is TableRole.DIMENSION
            and any(
                kpi_catalog.role_for_column(c.name) in {"status", "portfolio", "category"}
                for c in t.columns
            )
        ),
        None,
    )
    if dimension is not None:
        column = next(
            c for c in dimension.columns
            if kpi_catalog.role_for_column(c.name) in {"status", "portfolio", "category"}
        )
        cases.append(MeasureValidationCase(
            name=f"{name} sliced by {dimension.name}[{column.name}] sums to the total",
            filters={f"{dimension.name}[{column.name}]": "<each value>"},
            expected_source="For an additive measure the sum over every value of a "
                            "dimension column must equal the unsliced total. A shortfall "
                            "means rows are being filtered out by an unmatched key; an "
                            "excess means the relationship fans out.",
            comparison="equals", tolerance=0.02,
        ))
    return cases


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def generate_measures(
    *, model: Any, requirements: Any = None, kpi_ids: list[str] | None = None,
    qa_feedback: list[str] | None = None,
) -> dict[str, Any]:
    """Generate, validate and return measures for the requested KPIs."""
    from .validator import validate_measure

    model = model if isinstance(model, SemanticModel) else SemanticModel.model_validate(model)
    spec: RequirementsSpec | None = None
    if requirements is not None:
        spec = (
            requirements if isinstance(requirements, RequirementsSpec)
            else RequirementsSpec.model_validate(requirements)
        )
    if spec is None or not spec.kpis:
        return {
            "measures": [], "rejected": [],
            "notes": [
                "No approved KPI requirements were supplied, so no measure was generated. "
                "Measures are only ever created for a requested KPI -- the agent does not "
                "invent KPIs."
            ],
        }

    wanted = [
        k for k in spec.kpis
        if not kpi_ids or k.id in kpi_ids or k.name in (kpi_ids or [])
    ]
    notes: list[str] = []
    if qa_feedback:
        notes.append(
            f"Regenerating with {len(qa_feedback)} QA instruction(s): "
            + "; ".join(qa_feedback[:4])
        )

    # Dependency order: base measures first, so a ratio can reference them.
    priority = {
        "total_cost": 0, "total_budget": 0, "total_labour_hours": 0,
        "project_count": 0, "budget_variance": 1, "cost_per_hour": 1,
        "average_cost_per_project": 1, "project_completion_pct": 1,
        "budget_variance_pct": 2, "mom_change": 2, "ytd_cost": 2, "pct_of_total": 2,
    }

    def order(kpi: KpiRequirement) -> tuple[int, int]:
        shape = kpi_catalog.match(kpi.name)
        return (priority.get(shape.key if shape else "", 1), kpi.priority)

    measures: list[MeasureDefinition] = []
    rejected: list[dict[str, str]] = []
    built: dict[str, str] = {}  # lowercase shape key -> measure name

    for kpi in sorted(wanted, key=order):
        name = _measure_name_for(kpi)

        if not kpi.definition:
            rejected.append({
                "kpi": kpi.name, "name": name, "expression": "",
                "reason": "The KPI has no agreed definition, so no measure was generated. "
                          "Answer the open question about it first -- generating a formula "
                          "from a guessed definition produces a confidently wrong number.",
            })
            continue

        shape = kpi_catalog.match(kpi.name)
        key = shape.key if shape else ""
        built_lookup = {
            "total cost": built.get("total_cost"),
            "total budget": built.get("total_budget"),
            "budget variance": built.get("budget_variance"),
            "total labour hours": built.get("total_labour_hours"),
        }
        built_lookup = {k: v for k, v in built_lookup.items() if v}

        produced = None
        if key == "total_budget":
            produced = _budget_measure(kpi, model)
        elif key == "budget_variance":
            produced = _variance(kpi, model, built_lookup)
        elif key == "budget_variance_pct":
            produced = _variance_pct(kpi, model, built_lookup)
        elif key == "average_cost_per_project":
            produced = _average_per_entity(kpi, model, built_lookup)
        elif key == "project_completion_pct":
            produced = _average_of_rate(kpi, model)
        elif key == "mom_change":
            produced = _period_change(kpi, model, built_lookup)
        elif key == "ytd_cost":
            produced = _running_total(kpi, model, built_lookup)
        elif key == "project_count":
            produced = _distinct_count(kpi, model)
        elif key == "pct_of_total":
            produced = _percent_of_total(kpi, model, built_lookup)
        elif key == "cost_per_hour":
            produced = _ratio_of_roles(kpi, model)
        elif key == "total_labour_hours":
            produced = _simple_aggregation(
                kpi.model_copy(update={"numerator_hint": "hours"}), model
            )
        elif key == "total_cost":
            produced = _simple_aggregation(
                kpi.model_copy(update={"numerator_hint": "cost"}), model
            )
        else:
            produced = _simple_aggregation(kpi, model) or _ratio_of_roles(kpi, model)

        if produced is None:
            missing = _explain_missing(kpi, model, shape)
            rejected.append({
                "kpi": kpi.name, "name": name, "expression": "", "reason": missing,
            })
            continue

        expression, grounding, home_table, behaviour = produced

        if shape and shape.requires_date_table and not model.has_date_dimension:
            rejected.append({
                "kpi": kpi.name, "name": name, "expression": expression,
                "reason": "This KPI needs time intelligence, which requires a date "
                          "dimension. The model has none, so the measure was NOT generated "
                          "rather than generated in a form that would return wrong values.",
            })
            continue

        measure = MeasureDefinition(
            name=name, expression=expression,
            home_table=home_table or _default_home(model),
            kpi_id=kpi.id,
            description=_explanation(kpi, expression, grounding),
            format_string=kpi.format_string or shape.format_string if shape else "",
            display_folder=_folder_for(kpi),
            unit=kpi.unit,
            assumptions=_unique(
                (list(shape.assumptions) if shape else [])
                + [_currency_guard(model, grounding.get("value", Grounding()))[1]]
            ),
            aggregation_behaviour=behaviour,
            depends_on_measures=sorted({
                m.name for m in model.measures + measures
                if f"[{m.name}]" in expression
            }),
            depends_on_columns=sorted({
                g.reference for g in grounding.values() if g.resolved
            }),
            requires_date_table=bool(shape and shape.requires_date_table),
            confidence=kpi.confidence,
            provenance=Provenance(
                origin=Origin.DETERMINISTIC, produced_by="dax_agent",
                confidence=kpi.confidence,
                rationale=f"Generated from the '{key or 'generic'}' metric template, "
                          f"grounded in the approved model: "
                          + (", ".join(
                              f"{role}={g.reference}" for role, g in grounding.items()
                              if g.resolved
                          ) or "no column grounding required"),
                evidence=[
                    f"KPI definition: {kpi.definition[:160]}",
                    f"aggregation: {kpi.aggregation.value}",
                ] + ([f"derived from measure(s): "
                      f"{', '.join(built_lookup.values())}"] if built_lookup else []),
            ),
        )
        measure.validation_cases = _validation_cases(kpi, expression, model, grounding)

        # Validate before returning. A measure that fails its own gates is
        # recorded as rejected, not handed on as if it worked.
        probe_model = model.model_copy(
            update={"measures": model.measures + measures}
        )
        results = validate_measure(measure, probe_model)
        blocking = [r for r in results if r.status.is_blocking]
        if blocking:
            rejected.append({
                "kpi": kpi.name, "name": name, "expression": expression,
                "reason": "Generated but failed validation: "
                          + "; ".join(f"{r.test_name}: {r.actual}" for r in blocking),
            })
            continue

        measures.append(measure)
        if key:
            built[key] = name

    for item in rejected:
        notes.append(f"{item['name'] or item['kpi']}: {item['reason']}")

    return {"measures": measures, "rejected": rejected, "notes": notes}


def _unique(items: list[str]) -> list[str]:
    """Order-preserving de-duplication.

    The currency note can arrive both from the catalogue's assumptions and from
    the grounding check; printing it twice in the generated documentation reads
    as carelessness.
    """
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = (item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _default_home(model: SemanticModel) -> str:
    """Where to attach a measure that aggregates nothing directly.

    A pure measure expression (``[Total Budget] - [Total Cost]``) has no
    column of its own, but every measure needs a home table. The largest fact
    table is the sensible default: it is where a report author looks first, and
    attaching such a measure to a small or unrelated table makes the field list
    confusing.
    """
    facts = sorted(model.fact_tables, key=lambda t: -t.row_count)
    if facts:
        return facts[0].name
    return model.tables[0].name if model.tables else ""


def _folder_for(kpi: KpiRequirement) -> str:
    if kpi.time_intelligence:
        return "Time intelligence"
    if kpi.unit == "%":
        return "Ratios"
    if kpi.unit == "hours":
        return "Labour"
    return "Core"


def _explain_missing(
    kpi: KpiRequirement, model: SemanticModel, shape: Any
) -> str:
    """Say exactly which role could not be grounded, and where to look."""
    needed = list(getattr(shape, "needs_roles", ()) or [])
    if kpi.numerator_hint:
        needed.append(kpi.numerator_hint)
    if kpi.denominator_hint:
        needed.append(kpi.denominator_hint)
    missing = []
    for role in dict.fromkeys(needed):
        if role in {"variance", "date"}:
            continue
        if not _ground(model, role).resolved:
            missing.append(role)
    available = sorted({
        kpi_catalog.role_for_column(c.name)
        for t in model.tables for c in t.columns
        if kpi_catalog.role_for_column(c.name)
    })
    if missing:
        return (
            f"No measure was generated: the model has no column carrying "
            f"{', '.join(missing)}. Roles present in the model: "
            f"{', '.join(available) or 'none'}. Map the right source column to this role "
            f"in the Data Explorer and re-run."
        )
    if shape and shape.key in {"budget_variance", "budget_variance_pct",
                               "mom_change", "ytd_cost", "average_cost_per_project"}:
        return (
            "No measure was generated because the base measure(s) it builds on were not "
            "created. Fix those first; this measure depends on them."
        )
    return (
        "No measure template matched this KPI and its roles could not be grounded in the "
        "model, so nothing was generated."
    )
