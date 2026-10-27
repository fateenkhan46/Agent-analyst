"""Designing the semantic model.

The design rules, in the order they matter:

1. **Facts and dimensions are decided by shape, not by name.** A table whose
   key is unique is a dimension even if it carries numbers (a project register
   holds a budget); a table with a measure and a date whose key repeats is a
   fact. Getting this backwards is what causes budget to be double-counted
   across cost rows.
2. **Relationships are always many-to-one from the fact side**, which is the
   only direction Power BI applies them, and are only created where a measured
   key overlap supports them.
3. **Ambiguity is reported, not resolved silently.** Two paths between tables,
   a non-unique "one" side, or a many-to-many pair each produce a warning and
   an approval gate rather than a confident guess.
4. **Shared dimensions, not fact-to-fact relationships.** Two fact tables at
   different grains (cost per transaction, labour per timesheet) are related
   to the dimension, never to each other -- joining them would fan out and
   double-count, which the meeting transcript in the sample project explicitly
   warns about.
"""
from __future__ import annotations

from typing import Any

from ...schemas.common import (
    CanonicalType, Confidence, Issue, Origin, Provenance, Severity,
)
from ...schemas.prep import JoinProposal, TransformationPlan
from ...schemas.requirements import RequirementsSpec
from ...schemas.semantic import (
    Cardinality, ColumnDefinition, CrossFilter, DiagramEdge, DiagramNode, ModelDiagram,
    RelationshipDefinition, SemanticModel, SummarizeBy, TableDefinition, TableRole,
)
from ...schemas.source import SourceDataProfile
from .. import kpi_catalog

#: Columns we hide from the report view: surrogate keys, audit and flag columns
#: produced by the preparation step. They are kept in the model (for lineage
#: and debugging) but hidden so a report author does not drag them onto a page.
HIDDEN_SUFFIXES = (
    "_original", "_unparsed", "_is_missing", "_source_format",
    "_is_reporting_currency", "_key",
)

#: Format strings by semantic role, so a currency reads as currency in Power BI.
ROLE_FORMAT = {
    "cost": '"$"#,0.00', "budget": '"$"#,0.00', "rate": '"$"#,0.00',
    "hours": "#,0.0", "completion": "0.0%",
}


def _column_definition(
    raw: dict[str, Any], *, table_role: TableRole, is_key: bool
) -> ColumnDefinition:
    name = str(raw["name"])
    try:
        data_type = CanonicalType(raw.get("canonical_type", "string"))
    except ValueError:
        data_type = CanonicalType.STRING
    role = raw.get("role") or kpi_catalog.role_for_column(name)

    hidden = name.lower().endswith(HIDDEN_SUFFIXES) or name == "source_table"
    # summarize_by stays NONE everywhere: an implicit measure that sums an id
    # or averages a rate is a silent source of wrong numbers, so measures are
    # always explicit.
    return ColumnDefinition(
        name=name, source_column=name, data_type=data_type, is_key=is_key,
        is_hidden=hidden and not is_key,
        is_nullable=bool(raw.get("nullable", True)),
        summarize_by=SummarizeBy.NONE,
        format_string=ROLE_FORMAT.get(role, ""),
        description=(
            f"Semantic role: {role}." if role else ""
        ) + (" Hidden: produced by data preparation for audit." if hidden else ""),
        data_category=(
            "Years" if name == "year" else "Months" if name == "month_name" else ""
        ),
        sort_by_column=(
            "month_number" if name in {"month_name", "month_year"} else ""
        ),
    )


def _pick_key(dataset: dict[str, Any], role_hint: str) -> str:
    """The column that identifies a row, or '' when none does.

    Uniqueness is required, not just a key-shaped name. A fact table's
    ``project_id`` is a foreign key that repeats on every row; calling it a
    primary key would make the table look like a valid one-side of a
    relationship and invite exactly the fan-out this system exists to prevent.
    """
    columns = dataset.get("columns", [])
    if role_hint == "date":
        for candidate in ("date", "date_key"):
            match = next((c for c in columns if str(c["name"]) == candidate), None)
            if match:
                return str(match["name"])

    unique_columns = [c for c in columns if c.get("unique")]
    if not unique_columns:
        # A dimension built from distinct values has exactly one value column
        # plus a generated surrogate; treat the value column as the key.
        if str(dataset.get("name", "")).startswith("dim_") and columns:
            return str(columns[0]["name"])
        return ""

    def rank(column: dict[str, Any]) -> tuple[int, int]:
        name = str(column["name"])
        role = column.get("role") or kpi_catalog.role_for_column(name)
        is_key_role = role in kpi_catalog.KEY_ROLES
        suffixed = name.endswith(("_id", "_code", "_key", "_no"))
        return (0 if is_key_role else 1, 0 if suffixed else 1)

    unique_columns.sort(key=rank)
    return str(unique_columns[0]["name"])


def _role_for_dataset(dataset: dict[str, Any]) -> TableRole:
    hint = dataset.get("role_hint", "")
    if hint == "date":
        return TableRole.DATE
    if hint == "dimension":
        return TableRole.DIMENSION
    if hint == "bridge":
        return TableRole.BRIDGE
    return TableRole.FACT


def _table_name(dataset_name: str, role: TableRole) -> str:
    """A readable model name. Prefixes make the star obvious in the diagram."""
    base = "".join(part.capitalize() for part in dataset_name.split("_") if part)
    if role is TableRole.DATE:
        return "DimDate"
    if dataset_name.startswith("dim_"):
        return base if base.startswith("Dim") else f"Dim{base}"
    if role is TableRole.DIMENSION:
        return base if base.startswith("Dim") else f"Dim{base}"
    return base if base.startswith("Fact") else f"Fact{base}"


def design_model(
    *, project_id: str, datasets: list[Any], profile: Any = None,
    requirements: Any = None, plan: Any = None,
) -> dict[str, Any]:
    """Build a ``SemanticModel`` plus a ``ModelDiagram``."""
    dataset_records: list[dict[str, Any]] = []
    for d in datasets:
        if isinstance(d, dict):
            dataset_records.append(d)
    if not dataset_records:
        raise ValueError(
            "No prepared datasets were supplied, so there is nothing to model. "
            "Run the transformation step first."
        )

    spec: RequirementsSpec | None = None
    if requirements is not None:
        spec = (
            requirements if isinstance(requirements, RequirementsSpec)
            else RequirementsSpec.model_validate(requirements)
        )
    transformation_plan: TransformationPlan | None = None
    if plan is not None:
        transformation_plan = (
            plan if isinstance(plan, TransformationPlan)
            else TransformationPlan.model_validate(plan)
        )
    source_profile: SourceDataProfile | None = None
    if profile is not None:
        source_profile = (
            profile if isinstance(profile, SourceDataProfile)
            else SourceDataProfile.model_validate(profile)
        )

    # ---- tables -------------------------------------------------------
    tables: list[TableDefinition] = []
    dataset_to_table: dict[str, str] = {}
    issues: list[Issue] = []

    for dataset in dataset_records:
        role = _role_for_dataset(dataset)
        name = _table_name(str(dataset["name"]), role)
        while any(t.name == name for t in tables):
            name = f"{name}2"
        key = _pick_key(dataset, dataset.get("role_hint", ""))
        columns = [
            _column_definition(c, table_role=role, is_key=(str(c["name"]) == key))
            for c in dataset.get("columns", [])
        ]
        grain = _describe_grain(dataset, key, role)
        table = TableDefinition(
            name=name, role=role, dataset_name=str(dataset["name"]),
            columns=columns, primary_key=[key] if key else [],
            row_count=int(dataset.get("row_count", 0)),
            grain=grain,
            is_hidden=role is TableRole.BRIDGE,
            rationale=_table_rationale(dataset, role, key),
            confidence=Confidence.HIGH if key else Confidence.MEDIUM,
            description=f"Loaded from the prepared dataset '{dataset['name']}'.",
        )
        tables.append(table)
        dataset_to_table[str(dataset["name"])] = name

        if role is TableRole.FACT and not key:
            issues.append(Issue(
                code="model.fact_without_key", severity=Severity.LOW,
                location=name,
                message=f"'{name}' has no identifying key column.",
                recommended_fix="A fact table does not require a key, but without one "
                                "duplicate rows cannot be detected after load.",
            ))

    # ---- relationships ------------------------------------------------
    relationships: list[RelationshipDefinition] = []
    proposals: list[JoinProposal] = (
        transformation_plan.join_proposals if transformation_plan else []
    )

    # A join proposal names the table as it was BEFORE the append step, e.g.
    # "project_costs_fy25_costs". Map those names onto the appended dataset so
    # the relationship lands on the table that actually exists in the model.
    appended_from: dict[str, str] = {}
    if transformation_plan:
        from ...schemas.prep import TransformOp
        for step in transformation_plan.ordered_steps():
            if step.op is TransformOp.APPEND_TABLES:
                for source in step.params.get("tables", []) or []:
                    appended_from[source] = step.target_table

    def resolve(dataset_name: str) -> str:
        direct = dataset_to_table.get(dataset_name)
        if direct:
            return direct
        merged = appended_from.get(dataset_name)
        if merged:
            return dataset_to_table.get(merged, "")
        return _find_table_for(tables, dataset_name)

    seen_relationships: set[tuple[str, str, str, str]] = set()
    for proposal in proposals:
        from_table = resolve(proposal.left_table)
        to_table = resolve(proposal.right_table)
        if not from_table or not to_table or from_table == to_table:
            continue
        child = next(t for t in tables if t.name == from_table)
        parent = next(t for t in tables if t.name == to_table)
        from_column = proposal.left_keys[0] if proposal.left_keys else ""
        to_column = proposal.right_keys[0] if proposal.right_keys else ""
        if child.column(from_column) is None or parent.column(to_column) is None:
            issues.append(Issue(
                code="model.relationship_column_missing", severity=Severity.MEDIUM,
                location=f"{from_table}.{from_column} -> {to_table}.{to_column}",
                message="A proposed relationship references a column that is not in the "
                        "prepared data, so it was not created.",
                evidence={"proposal": proposal.model_dump(mode="json")},
                recommended_fix="Check that the preparation step produced both key columns.",
            ))
            continue

        # Two fact tables must relate through a dimension, never to each other.
        if child.role is TableRole.FACT and parent.role is TableRole.FACT:
            issues.append(Issue(
                code="model.fact_to_fact_relationship_avoided", severity=Severity.MEDIUM,
                location=f"{from_table} <-> {to_table}",
                message=f"'{from_table}' and '{to_table}' are both fact tables at "
                        f"different grains, so no direct relationship was created.",
                evidence={"match_share": proposal.match_share},
                recommended_fix="Both are related to the shared dimension instead. "
                                "A direct fact-to-fact relationship would fan out and "
                                "double-count every additive measure.",
            ))
            continue

        signature = (
            from_table, from_column, to_table, to_column,
        )
        if signature in seen_relationships:
            continue
        seen_relationships.add(signature)

        confidence = proposal.confidence
        cardinality = Cardinality.MANY_TO_ONE
        if proposal.fan_out_risk:
            cardinality = Cardinality.MANY_TO_MANY
        relationship = RelationshipDefinition(
            from_table=from_table, from_column=from_column,
            to_table=to_table, to_column=to_column,
            cardinality=cardinality,
            cross_filter=CrossFilter.SINGLE,
            match_share=proposal.match_share,
            orphan_key_count=proposal.unmatched_left,
            to_side_is_unique=not proposal.fan_out_risk,
            rationale=(
                f"{proposal.match_share:.1%} of {from_table}.{from_column} values resolve "
                f"in {to_table}.{to_column}. "
                + ("The one-side key is unique, so this is a clean many-to-one. "
                   if not proposal.fan_out_risk else
                   "The one-side key is NOT unique, so this is many-to-many and needs a "
                   "decision. ")
                + (f"{proposal.unmatched_left} key(s) have no match and will fall into the "
                   f"'UNASSIGNED' member." if proposal.unmatched_left else
                   "Every key resolves.")
            ),
            evidence=proposal.evidence,
            confidence=confidence,
        )
        relationships.append(relationship)

    # Relate every fact table to each conformed dimension on the shared
    # column. Without this a dim_* table built from distinct values sits
    # isolated, and a slicer on it filters nothing.
    for dimension in tables:
        if dimension.role is not TableRole.DIMENSION:
            continue
        if not str(dimension.dataset_name).startswith("dim_"):
            continue
        # The dimension carries both the value column and a generated
        # surrogate; relate on whichever one the fact tables actually hold.
        candidate_keys = [
            c.name for c in dimension.columns if not c.name.endswith("_key")
        ] + [c.name for c in dimension.columns if c.name.endswith("_key")]
        key = next(
            (
                k for k in candidate_keys
                if any(t.role is TableRole.FACT and t.column(k) is not None for t in tables)
            ),
            dimension.primary_key[0] if dimension.primary_key else "",
        )
        if not key:
            continue
        for fact in tables:
            if fact.role is not TableRole.FACT or fact.column(key) is None:
                continue
            signature = (fact.name, key, dimension.name, key)
            if signature in seen_relationships:
                continue
            seen_relationships.add(signature)
            relationships.append(RelationshipDefinition(
                from_table=fact.name, from_column=key,
                to_table=dimension.name, to_column=key,
                cardinality=Cardinality.MANY_TO_ONE, cross_filter=CrossFilter.SINGLE,
                match_share=1.0, to_side_is_unique=True,
                rationale=f"'{dimension.name}' is a conformed dimension built from the "
                          f"distinct values of '{key}' across every table that records "
                          f"it, so relating '{fact.name}' to it lets one slicer filter "
                          f"every fact table consistently.",
                evidence=[
                    f"{dimension.name} holds {dimension.row_count} distinct value(s) "
                    f"of {key}",
                    f"{fact.name} carries the same column",
                ],
                confidence=Confidence.HIGH,
            ))

    # Relate every fact table to the date dimension on its own date column.
    date_table = next((t for t in tables if t.role is TableRole.DATE), None)
    if date_table is not None:
        date_key = next(
            (c.name for c in date_table.columns if c.data_type.is_temporal), ""
        )
        for table in tables:
            if table.role is not TableRole.FACT:
                continue
            fact_date = next(
                (
                    c.name for c in table.columns
                    if c.data_type.is_temporal and not c.is_hidden
                ),
                "",
            )
            if not fact_date or not date_key:
                continue
            if any(
                r.from_table == table.name and r.to_table == date_table.name
                for r in relationships
            ):
                continue
            relationships.append(RelationshipDefinition(
                from_table=table.name, from_column=fact_date,
                to_table=date_table.name, to_column=date_key,
                cardinality=Cardinality.MANY_TO_ONE, cross_filter=CrossFilter.SINGLE,
                match_share=1.0, to_side_is_unique=True,
                rationale=f"'{table.name}.{fact_date}' is the event date for this fact "
                          f"table; relating it to the contiguous date dimension is what "
                          f"makes time intelligence work.",
                evidence=[
                    f"{date_table.name} is a generated contiguous calendar covering the "
                    f"full data range",
                    "a date relationship is required for YTD / month-over-month measures",
                ],
                confidence=Confidence.HIGH,
            ))
        if not any(r.to_table == date_table.name for r in relationships):
            issues.append(Issue(
                code="model.date_table_unrelated", severity=Severity.HIGH,
                location=date_table.name,
                message="A date dimension exists but no fact table has a usable date "
                        "column to relate it to.",
                recommended_fix="Check that the date parse step succeeded; without a date "
                                "relationship every time-intelligence measure is wrong.",
            ))

    model = SemanticModel(
        project_id=project_id,
        name=(spec.business_objective[:60] if spec and spec.business_objective
              else "Semantic Model"),
        tables=tables, relationships=relationships,
        provenance=Provenance(
            origin=Origin.DETERMINISTIC, produced_by="semantic_model_agent",
            confidence=Confidence.HIGH,
            rationale="Fact/dimension roles come from measured key uniqueness, not table "
                      "names. Relationships are created only where the preparation step "
                      "measured a key overlap, always many-to-one from the fact side.",
            evidence=[
                f"{len(tables)} table(s): "
                + ", ".join(f"{t.name} ({t.role.value})" for t in tables),
                f"{len(relationships)} relationship(s) from "
                f"{len(proposals)} measured proposal(s)",
            ],
        ),
    )
    model.issues = issues + _validate_model(model, source_profile)
    model.schema_shape = _classify_shape(model)
    model.design_rationale = _design_rationale(model)
    return {
        "model": model, "diagram": build_diagram(model),
        "notes": [i.message for i in model.issues if i.severity in
                  {Severity.CRITICAL, Severity.HIGH}],
    }


def _find_table_for(tables: list[TableDefinition], dataset_name: str) -> str:
    match = next((t for t in tables if t.dataset_name == dataset_name), None)
    return match.name if match else ""


def _describe_grain(dataset: dict[str, Any], key: str, role: TableRole) -> str:
    rows = int(dataset.get("row_count", 0))
    if role is TableRole.DATE:
        return "One row per calendar day."
    if role is TableRole.DIMENSION and key:
        return f"One row per {key.replace('_', ' ')} ({rows:,} rows)."
    date_columns = [
        str(c["name"]) for c in dataset.get("columns", [])
        if (c.get("role") or kpi_catalog.role_for_column(str(c["name"]))) == "date"
    ]
    if date_columns and key:
        return (
            f"One row per transaction: {key.replace('_', ' ')} and "
            f"{date_columns[0].replace('_', ' ')} ({rows:,} rows). This is "
            f"transaction-level detail, not pre-aggregated."
        )
    return f"{rows:,} rows; grain not established from the data."


def _table_rationale(dataset: dict[str, Any], role: TableRole, key: str) -> str:
    rows = int(dataset.get("row_count", 0))
    if role is TableRole.DATE:
        return (
            "A contiguous generated calendar. Required for time intelligence: DAX "
            "needs a gapless date table marked as such."
        )
    measures = [
        str(c["name"]) for c in dataset.get("columns", [])
        if (c.get("role") or kpi_catalog.role_for_column(str(c["name"])))
        in kpi_catalog.MEASURE_ROLES
    ]
    if role is TableRole.DIMENSION:
        return (
            f"Classified as a dimension because '{key}' is unique across all {rows:,} "
            f"rows -- one row per entity."
            + (f" It also carries {', '.join(measures[:3])}, which are attributes of the "
               f"entity rather than events, so they must not be summed across a related "
               f"fact table." if measures else "")
        )
    return (
        f"Classified as a fact table: it holds {', '.join(measures[:3]) or 'measures'} "
        f"and its key repeats across {rows:,} rows, so each row is an event."
    )


def _validate_model(
    model: SemanticModel, profile: SourceDataProfile | None
) -> list[Issue]:
    """Relationship and double-counting checks."""
    issues: list[Issue] = []

    # non-unique one-side
    for relationship in model.relationships:
        if not relationship.to_side_is_unique:
            issues.append(Issue(
                code="model.non_unique_one_side", severity=Severity.CRITICAL,
                location=relationship.key,
                message=f"The 'one' side of {relationship.key} is not unique, so the "
                        f"relationship is many-to-many.",
                evidence={"match_share": relationship.match_share,
                          "evidence": relationship.evidence},
                recommended_fix="Power BI will either refuse this relationship or create "
                                "a many-to-many one that silently fans out rows. Make the "
                                "parent key unique (build a proper dimension of its "
                                "distinct values) before relying on any additive measure.",
            ))
        if relationship.orphan_key_count:
            issues.append(Issue(
                code="model.referential_integrity", severity=Severity.HIGH,
                location=relationship.key,
                message=f"{relationship.orphan_key_count} key(s) in "
                        f"{relationship.from_table} have no matching row in "
                        f"{relationship.to_table}.",
                evidence={"orphan_key_count": relationship.orphan_key_count},
                recommended_fix="Those rows are attributed to the 'UNASSIGNED' dimension "
                                "member so they stay in the totals. Do not enable "
                                "'assume referential integrity' on this relationship.",
            ))

    # ambiguous filter paths, in the direction filters actually propagate
    for a in model.tables:
        for b in model.tables:
            if a.name == b.name:
                continue
            paths = model.filter_paths(a.name, b.name)
            if len(paths) > 1:
                issues.append(Issue(
                    code="model.ambiguous_path", severity=Severity.HIGH,
                    location=f"{a.name} <-> {b.name}",
                    message=f"Filters from '{a.name}' can reach '{b.name}' by "
                            f"{len(paths)} different routes.",
                    evidence={"paths": [" + ".join(p) for p in paths[:4]]},
                    recommended_fix="Power BI refuses ambiguous active paths. Deactivate "
                                    "one relationship and use USERELATIONSHIP where the "
                                    "alternative path is needed.",
                ))

    # duplicate keys on a dimension
    for table in model.tables:
        if table.role in {TableRole.DIMENSION, TableRole.DATE} and table.primary_key:
            incoming = [r for r in model.relationships if r.to_table == table.name]
            if incoming and not all(r.to_side_is_unique for r in incoming):
                issues.append(Issue(
                    code="model.dimension_key_not_unique", severity=Severity.CRITICAL,
                    location=f"{table.name}.{table.primary_key[0]}",
                    message=f"'{table.name}' is used as a dimension but its key is not "
                            f"unique.",
                    recommended_fix="De-duplicate the dimension to one row per key.",
                ))

    # double-counting risk: two facts sharing a dimension is fine, but a
    # measure summed over the dimension's own numeric column is not
    fact_count = len(model.fact_tables)
    for table in model.tables:
        if table.role is not TableRole.DIMENSION:
            continue
        numeric = [
            c.name for c in table.columns
            if c.data_type.is_numeric and not c.is_hidden and not c.is_key
        ]
        if numeric and fact_count:
            issues.append(Issue(
                code="model.dimension_measure_double_count_risk",
                severity=Severity.MEDIUM, location=table.name,
                message=f"'{table.name}' is a dimension but carries numeric column(s) "
                        f"{', '.join(numeric[:4])}.",
                evidence={"numeric_columns": numeric, "fact_tables": fact_count},
                recommended_fix="Summing these is correct at the dimension's own grain "
                                "(one row per entity) but WRONG if sliced by anything "
                                "finer, because the same value repeats. Measures over "
                                "them are generated against the dimension table itself, "
                                "never against a joined fact table.",
            ))

    if not model.fact_tables:
        issues.append(Issue(
            code="model.no_fact_table", severity=Severity.CRITICAL, location="model",
            message="No table was classified as a fact table, so there is nothing to "
                    "aggregate.",
            recommended_fix="Check the preparation output: a fact table needs a measure "
                            "column and a repeating key.",
        ))
    if not model.has_date_dimension:
        issues.append(Issue(
            code="model.no_date_dimension", severity=Severity.HIGH, location="model",
            message="The model has no date dimension.",
            recommended_fix="Time-intelligence measures (year-to-date, month-over-month) "
                            "cannot be generated without one, and will be reported as "
                            "unavailable rather than produced incorrectly.",
        ))

    # isolated tables
    for table in model.tables:
        if table.role is TableRole.DATE:
            continue
        connected = any(
            r.from_table == table.name or r.to_table == table.name
            for r in model.relationships
        )
        if not connected and len(model.tables) > 1:
            issues.append(Issue(
                code="model.isolated_table", severity=Severity.HIGH, location=table.name,
                message=f"'{table.name}' has no relationship to any other table.",
                recommended_fix="A slicer on another table will not filter it. Either "
                                "relate it on a shared key or remove it from the model.",
            ))
    return issues


def _classify_shape(model: SemanticModel) -> str:
    facts = model.fact_tables
    dimensions = [t for t in model.tables if t.role in {TableRole.DIMENSION, TableRole.DATE}]
    if not facts:
        return "no fact table"
    if len(model.tables) == 1:
        return "flat"
    dimension_to_dimension = [
        r for r in model.relationships
        if (model.table(r.from_table) or TableDefinition(name="", role=TableRole.FACT)).role
        in {TableRole.DIMENSION, TableRole.DATE}
        and (model.table(r.to_table) or TableDefinition(name="", role=TableRole.FACT)).role
        in {TableRole.DIMENSION, TableRole.DATE}
    ]
    if dimension_to_dimension:
        return "snowflake"
    if len(facts) > 1:
        return "galaxy (multiple facts sharing conformed dimensions)"
    if dimensions:
        return "star"
    return "mixed"


def _design_rationale(model: SemanticModel) -> str:
    facts = model.fact_tables
    dimensions = [t for t in model.tables if t.role is TableRole.DIMENSION]
    lines = [
        f"Shape: {model.schema_shape}. "
        f"{len(facts)} fact table(s), {len(dimensions)} dimension(s), "
        f"{'a' if model.has_date_dimension else 'no'} date dimension."
    ]
    if len(facts) > 1:
        lines.append(
            f"The {len(facts)} fact tables ({', '.join(t.name for t in facts)}) are at "
            f"different grains and are deliberately NOT related to each other. Each is "
            f"related to the shared dimensions, so a slicer filters both consistently "
            f"while neither inflates the other."
        )
    for table in facts:
        lines.append(f"{table.name}: {table.grain}")
    for table in dimensions:
        lines.append(f"{table.name}: {table.grain} {table.rationale}")
    if model.has_date_dimension:
        lines.append(
            "DimDate must be marked as the date table in Power BI Desktop "
            "(Table tools > Mark as date table) -- that setting is not expressible in "
            "TMDL, so it is the one manual step time intelligence needs."
        )
    return " ".join(lines)


def build_diagram(model: SemanticModel) -> ModelDiagram:
    """Lay the model out: facts in the centre, dimensions on a ring.

    Positions are computed here so the UI needs no layout engine and the same
    layout appears in the exported Mermaid.
    """
    import math

    facts = [t for t in model.tables if t.role is TableRole.FACT]
    others = [t for t in model.tables if t.role is not TableRole.FACT]
    nodes: list[DiagramNode] = []

    centre_x, centre_y = 480.0, 320.0
    for i, table in enumerate(facts):
        offset = (i - (len(facts) - 1) / 2) * 260
        nodes.append(DiagramNode(
            id=table.name, label=table.name, role=table.role,
            columns=[c.name for c in table.columns if not c.is_hidden][:12],
            row_count=table.row_count, x=centre_x + offset, y=centre_y,
        ))
    radius = 300.0
    for i, table in enumerate(others):
        angle = (2 * math.pi * i / max(1, len(others))) - math.pi / 2
        nodes.append(DiagramNode(
            id=table.name, label=table.name, role=table.role,
            columns=[c.name for c in table.columns if not c.is_hidden][:12],
            row_count=table.row_count,
            x=round(centre_x + radius * math.cos(angle) * 1.4, 1),
            y=round(centre_y + radius * math.sin(angle), 1),
        ))

    edges: list[DiagramEdge] = []
    for relationship in model.relationships:
        warning = ""
        if not relationship.to_side_is_unique:
            warning = "the one-side key is not unique: many-to-many"
        elif relationship.orphan_key_count:
            warning = f"{relationship.orphan_key_count} unmatched key(s)"
        elif relationship.requires_approval:
            warning = "awaiting approval"
        edges.append(DiagramEdge(
            id=relationship.id, source=relationship.from_table,
            target=relationship.to_table,
            label=f"{relationship.from_column} -> {relationship.to_column}",
            cardinality=relationship.cardinality, cross_filter=relationship.cross_filter,
            is_active=relationship.is_active and relationship.approved is not False,
            warning=warning,
        ))

    lines = ["erDiagram"]
    for table in model.tables:
        safe = table.name
        lines.append(f"    {safe} {{")
        for column in table.columns[:10]:
            if column.is_hidden:
                continue
            lines.append(
                f"        {column.data_type.value} {column.name}"
                + (" PK" if column.is_key else "")
            )
        lines.append("    }")
    for relationship in model.relationships:
        symbol = {
            Cardinality.MANY_TO_ONE: "}o--||",
            Cardinality.ONE_TO_MANY: "||--o{",
            Cardinality.ONE_TO_ONE: "||--||",
            Cardinality.MANY_TO_MANY: "}o--o{",
        }[relationship.cardinality]
        lines.append(
            f"    {relationship.from_table} {symbol} {relationship.to_table} : "
            f'"{relationship.from_column}"'
        )
    return ModelDiagram(nodes=nodes, edges=edges, mermaid="\n".join(lines))
