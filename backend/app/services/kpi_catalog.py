"""A catalogue of well-understood BI metric shapes.

Why this exists: asking a language model "what does Budget Variance mean?"
invites a plausible-sounding but arbitrary answer, and the answer may differ
between runs. The shapes below are standard, stable BI definitions. Matching a
requested KPI name against them gives us a *defensible* starting definition,
the right aggregation, a sensible format string, and -- crucially -- the
knowledge of which KPIs need a denominator or a date table.

Anything that does not match a catalogue entry is **not invented**: it becomes
a KPI with an empty definition and ``needs_confirmation=True``, which raises a
targeted question to the user.

A meeting decision always wins over the catalogue default: if a stakeholder
says variance is actual-minus-budget, that is recorded and used, and the
catalogue's sign convention is overridden.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..schemas.requirements import AggregationKind


@dataclass(frozen=True)
class KpiShape:
    key: str
    #: Regexes matched against the normalised KPI name.
    patterns: tuple[str, ...]
    aggregation: AggregationKind
    definition: str
    #: Semantic roles this KPI needs from the model, e.g. ("cost",) or
    #: ("budget", "cost"). Resolved against mapped canonical columns.
    needs_roles: tuple[str, ...] = ()
    numerator_role: str = ""
    denominator_role: str = ""
    unit: str = ""
    format_string: str = ""
    time_intelligence: tuple[str, ...] = ()
    higher_is_better: bool | None = None
    requires_date_table: bool = False
    #: Notes that become ``MeasureDefinition.assumptions``.
    assumptions: tuple[str, ...] = ()
    aggregation_behaviour: str = "additive"


CATALOGUE: tuple[KpiShape, ...] = (
    KpiShape(
        key="total_cost",
        patterns=(r"^total cost$", r"^actual cost$", r"^cost$", r"^total actuals?$",
                  r"^total spend$", r"^actual spend$"),
        aggregation=AggregationKind.SUM, needs_roles=("cost",), numerator_role="cost",
        unit="currency", format_string='"$"#,0.00',
        definition="The sum of all cost amounts in the cost fact table.",
        higher_is_better=False,
        assumptions=("All cost rows are in a single reporting currency; rows in another "
                     "currency are excluded and reported separately.",),
    ),
    KpiShape(
        key="total_budget",
        patterns=(r"^total budget$", r"^budget$", r"^approved budget$", r"^planned cost$"),
        aggregation=AggregationKind.SUM, needs_roles=("budget",), numerator_role="budget",
        unit="currency", format_string='"$"#,0.00',
        definition="The sum of approved budget, taken from the project dimension.",
        aggregation_behaviour=(
            "additive across projects, but NOT across any dimension finer than the "
            "project (budget is held once per project, so slicing by cost category or "
            "date repeats the same value)"
        ),
        assumptions=("Budget is held once per project in the register, so it does not "
                     "vary by date or cost category.",),
    ),
    KpiShape(
        key="budget_variance",
        patterns=(r"^budget variance$", r"^variance$", r"^cost variance$",
                  r"^budget vs actual$", r"^under/over budget$"),
        aggregation=AggregationKind.VARIANCE,
        needs_roles=("budget", "cost"), numerator_role="budget", denominator_role="cost",
        unit="currency", format_string='"$"#,0.00;("$"#,0.00)',
        definition="Total Budget minus Total Cost. A positive value means under budget.",
        higher_is_better=True,
        assumptions=("Sign convention: Budget - Cost, so positive is favourable. "
                     "Confirm this with the report owner -- the opposite convention is "
                     "equally common and silently inverts every conditional format.",),
    ),
    KpiShape(
        key="budget_variance_pct",
        patterns=(r"^budget variance %$", r"^budget variance percent(?:age)?$",
                  r"^variance %$", r"^variance percent(?:age)?$", r"^% variance$"),
        aggregation=AggregationKind.RATIO,
        needs_roles=("budget", "cost"), numerator_role="variance", denominator_role="budget",
        unit="%", format_string="0.0%",
        definition="Budget Variance divided by Total Budget.",
        higher_is_better=True, aggregation_behaviour="non-additive (a ratio)",
        assumptions=("Divided by budget, not by actual cost. Uses DIVIDE so a zero or "
                     "missing budget yields blank rather than an error or infinity.",),
    ),
    KpiShape(
        key="total_labour_hours",
        patterns=(r"^total (?:labour|labor) hours$", r"^(?:labour|labor) hours$",
                  r"^total hours$", r"^hours worked$", r"^effort$"),
        aggregation=AggregationKind.SUM, needs_roles=("hours",), numerator_role="hours",
        unit="hours", format_string="#,0.0",
        definition="The sum of timesheet hours, normalised to hours.",
        assumptions=("Source hours appear in several units (decimal hours, minutes, "
                     "hh:mm) and are normalised to decimal hours during preparation.",),
    ),
    KpiShape(
        key="average_cost_per_project",
        patterns=(r"^average cost per project$", r"^avg cost per project$",
                  r"^mean cost per project$", r"^cost per project$"),
        aggregation=AggregationKind.RATIO,
        needs_roles=("cost", "project_key"),
        numerator_role="cost", denominator_role="project_key",
        unit="currency", format_string='"$"#,0.00',
        definition="Total Cost divided by the number of distinct projects that have cost.",
        higher_is_better=False, aggregation_behaviour="non-additive (a ratio)",
        assumptions=("The denominator is a DISTINCTCOUNT of projects appearing in the "
                     "cost table, not a row count of the project register -- a project "
                     "with no cost would otherwise drag the average down.",),
    ),
    KpiShape(
        key="project_completion_pct",
        patterns=(r"^project completion %$", r"^project completion percent(?:age)?$",
                  r"^completion %$", r"^percent complete$", r"^% complete$", r"^progress$"),
        aggregation=AggregationKind.AVERAGE, needs_roles=("completion",),
        numerator_role="completion", unit="%", format_string="0.0%",
        definition="The average reported completion percentage across projects in context.",
        higher_is_better=True, aggregation_behaviour="non-additive (an average of a rate)",
        assumptions=("A plain average weights every project equally regardless of size. "
                     "A budget-weighted average is usually more meaningful; this is "
                     "offered as an improvement recommendation rather than assumed.",),
    ),
    KpiShape(
        key="mom_change",
        patterns=(r"^month[- ]over[- ]month.*$", r"^mom .*$", r".*month on month.*",
                  r"^monthly change$", r"^cost change$"),
        aggregation=AggregationKind.PERIOD_CHANGE, needs_roles=("cost", "date"),
        numerator_role="cost", unit="currency", format_string='"$"#,0.00;("$"#,0.00)',
        definition="The change in the measure versus the equivalent prior month.",
        time_intelligence=("mom",), requires_date_table=True,
        aggregation_behaviour="non-additive (a period-over-period difference)",
        assumptions=("Requires a contiguous, marked date table. Returns blank for the "
                     "first month in context rather than showing the full value as growth.",),
    ),
    KpiShape(
        key="ytd_cost",
        patterns=(r"^year[- ]to[- ]date.*$", r"^ytd.*$", r".*year to date.*",
                  r"^cumulative cost$"),
        aggregation=AggregationKind.RUNNING_TOTAL, needs_roles=("cost", "date"),
        numerator_role="cost", unit="currency", format_string='"$"#,0.00',
        definition="Cumulative total from the start of the year to the end of the context.",
        time_intelligence=("ytd",), requires_date_table=True,
        aggregation_behaviour="semi-additive (cumulative over time)",
        assumptions=("Uses the calendar year unless a fiscal year start is specified.",),
    ),
    KpiShape(
        key="project_count",
        patterns=(r"^(?:number of |#? ?of )?projects?( count)?$", r"^project count$",
                  r"^active projects$"),
        aggregation=AggregationKind.DISTINCT_COUNT, needs_roles=("project_key",),
        numerator_role="project_key", unit="count", format_string="#,0",
        definition="The number of distinct projects in context.",
    ),
    KpiShape(
        key="cost_per_hour",
        patterns=(r"^cost per hour$", r"^(?:labour|labor) rate$", r"^effective rate$"),
        aggregation=AggregationKind.RATIO, needs_roles=("cost", "hours"),
        numerator_role="cost", denominator_role="hours",
        unit="currency", format_string='"$"#,0.00',
        definition="Total Cost divided by Total Labour Hours.",
        aggregation_behaviour="non-additive (a ratio)",
        assumptions=("Mixes two fact tables. Only meaningful where both are filtered to "
                     "the same project and period.",),
    ),
    KpiShape(
        key="pct_of_total",
        patterns=(r"^.*% of total.*$", r"^share of .*$", r"^.*contribution.*$"),
        aggregation=AggregationKind.PERCENT_OF_TOTAL, needs_roles=("cost",),
        numerator_role="cost", unit="%", format_string="0.0%",
        definition="The measure in context divided by the same measure with the "
                   "category filter removed.",
        aggregation_behaviour="non-additive (a ratio to an outer total)",
    ),
)


def _normalise(name: str) -> str:
    n = name.strip().lower()
    n = n.replace("percentage", "percent")
    n = re.sub(r"\bpct\b", "percent", n)
    n = re.sub(r"\s+", " ", n)
    # unify the various ways people write a percent KPI
    n = re.sub(r"\bpercent\b", "percent", n)
    n = n.replace(" percent", " %").replace("percent ", "% ")
    if n.endswith(" %)"):
        n = n[:-3] + " %"
    return n.strip()


def match(name: str) -> KpiShape | None:
    """Find the catalogue shape for a KPI name, or ``None``.

    ``None`` is a meaningful result: the caller must raise a question rather
    than inventing a definition.
    """
    n = _normalise(name)
    for shape in CATALOGUE:
        for pattern in shape.patterns:
            if re.fullmatch(pattern, n) or re.fullmatch(pattern, n.replace(" %", " percent")):
                return shape
    # second pass: allow a leading/trailing qualifier, e.g. "FY25 Total Cost"
    stripped = re.sub(r"^(fy\d{2,4}|total|overall|monthly|annual)\s+", "", n)
    stripped = re.sub(r"\s+(by [a-z ]+)$", "", stripped)
    if stripped != n:
        return match(stripped)
    return None


#: Canonical column roles the prep agent tries to identify, and the name
#: fragments that indicate each. Used both for KPI grounding and for column
#: mapping. Ordered: earlier entries win a tie.
ROLE_HINTS: dict[str, tuple[str, ...]] = {
    "cost": ("cost amount", "cost_amount", "amount_usd", "actual cost", "cost", "amount",
             "spend", "expense", "value"),
    "budget": ("approved budget", "budget", "planned cost", "baseline"),
    "hours": ("hours worked", "hours_worked", "hours", "effort", "duration", "time spent"),
    "completion": ("percent complete", "completion", "progress", "pct complete"),
    "project_key": ("project code", "project id", "proj id", "project_id", "project",
                    "proj", "wbs"),
    "date": ("cost date", "transaction date", "week ending", "date", "period", "month",
             "posting date"),
    "category": ("cost category", "category", "type", "classification"),
    "status": ("status", "state", "phase"),
    "portfolio": ("portfolio", "programme", "program", "division", "segment"),
    "manager": ("project manager", "manager", "owner", "pm"),
    "employee_key": ("employee id", "employee_id", "employee", "staff", "resource", "person"),
    "vendor": ("vendor", "supplier", "contractor"),
    "rate": ("hourly rate", "rate", "unit price"),
    "department": ("dept", "department", "team", "function"),
    "invoice": ("invoice", "document no", "voucher"),
    "name": ("project name", "name", "description", "title"),
}


def role_for_column(column_name: str) -> str:
    """Best-guess semantic role for a column name, or ''.

    Longest hint wins, so "approved budget" beats a bare "budget" and
    "project name" is not mistaken for the project key.
    """
    low = re.sub(r"[^a-z0-9 ]+", " ", column_name.lower()).strip()
    low = re.sub(r"\s+", " ", low)
    best_role, best_len = "", 0
    for role, hints in ROLE_HINTS.items():
        for hint in hints:
            if hint in low and len(hint) > best_len:
                best_role, best_len = role, len(hint)
    return best_role


#: A role that is a *measure* (summed) rather than an attribute.
MEASURE_ROLES = {"cost", "budget", "hours", "rate", "completion"}
#: Roles that identify an entity -- candidate relationship keys.
KEY_ROLES = {"project_key", "employee_key", "invoice", "vendor"}
#: Roles that become dimension attributes.
ATTRIBUTE_ROLES = {"category", "status", "portfolio", "manager", "department", "name"}
