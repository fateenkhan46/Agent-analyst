"""Writing TMDL -- the text format Power BI Desktop reads a model from.

TMDL is whitespace-significant: object nesting is expressed by indentation
(tabs, by Microsoft's own convention), and a multi-line expression is
introduced by ``=`` followed by a deeper-indented block. Getting the
indentation wrong produces a project that Desktop refuses to open, so all of
it goes through ``_block`` and ``_expression`` rather than being assembled by
hand at each call site.

What is emitted, and why it is enough: Desktop compiles this TMDL into a live
model, runs the Power Query partitions against the data, and renders the
report. The one thing TMDL cannot express is "mark as date table", which is a
Desktop UI action -- so that is stated as the single manual step in
OPEN_IN_POWER_BI.md rather than glossed over.
"""
from __future__ import annotations

import re
from typing import Iterable

from ...schemas.semantic import (
    Cardinality, ColumnDefinition, CrossFilter, MeasureDefinition, RelationshipDefinition,
    SemanticModel, SummarizeBy, TableDefinition, TableRole,
)

INDENT = "\t"

#: TMDL reserves these, and a name containing anything outside this set must be
#: quoted. Keeping model names simple avoids most of it, but a user-supplied
#: column name can be anything.
_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_name(name: str) -> str:
    """Quote an object name if TMDL needs it."""
    if _SAFE_NAME.match(name):
        return name
    escaped = name.replace("'", "''")
    return f"'{escaped}'"


def quote_string(value: str) -> str:
    """A TMDL string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").split("\n")


def _block(header: str, body: Iterable[str], *, depth: int = 0) -> list[str]:
    """A TMDL object: a header line, then its properties one level deeper."""
    out = [f"{INDENT * depth}{header}"]
    for line in body:
        out.append(f"{INDENT * (depth + 1)}{line}" if line else "")
    return out


def _expression(name: str, expression: str, *, depth: int) -> list[str]:
    """A property whose value is an expression.

    A single line goes inline after ``=``. A multi-line expression must be
    introduced by ``= ```` and then indented one level deeper -- this is where
    hand-written TMDL usually goes wrong.
    """
    body = expression.strip()
    if "\n" not in body:
        return [f"{INDENT * depth}{name} = {body}"]
    out = [f"{INDENT * depth}{name} = ```"]
    for line in _lines(body):
        out.append(f"{INDENT * (depth + 1)}{line}" if line.strip() else "")
    out.append(f"{INDENT * (depth + 1)}```")
    return out


def _annotation(name: str, value: str, *, depth: int) -> list[str]:
    return [f"{INDENT * depth}annotation {name} = {value}"]


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------
def column_tmdl(column: ColumnDefinition, *, depth: int = 1) -> list[str]:
    """One column. A TMDL description is a ``///`` comment above the object."""
    lines: list[str] = []
    if column.description:
        for chunk in _wrap(column.description, 92):
            lines.append(f"{INDENT * depth}/// {chunk}")

    body: list[str] = [f"dataType: {column.powerbi_type}"]
    if column.is_key:
        body.append("isKey")
    if column.is_hidden:
        body.append("isHidden")
    if not column.is_nullable:
        body.append("isNullable: false")
    body.append(f"summarizeBy: {column.summarize_by.value}")
    body.append(f"sourceColumn: {quote_name(column.source_column or column.name)}")
    if column.format_string:
        body.append(f"formatString: {column.format_string}")
    if column.data_category:
        body.append(f"dataCategory: {column.data_category}")
    if column.sort_by_column:
        body.append(f"sortByColumn: {quote_name(column.sort_by_column)}")

    lines.extend(_block(f"column {quote_name(column.name)}", body, depth=depth))
    lines.append("")
    lines.extend(_annotation("SummarizationSetBy", "Automatic", depth=depth + 1))
    lines.append("")
    return lines


def measure_tmdl(measure: MeasureDefinition, *, depth: int = 1) -> list[str]:
    lines: list[str] = []
    # The explanation, assumptions and aggregation behaviour go into the model
    # itself, so a Power BI developer who opens the project sees why the
    # measure is written the way it is.
    description_parts = [measure.description]
    if measure.aggregation_behaviour:
        description_parts.append(f"Behaviour: {measure.aggregation_behaviour}")
    for assumption in measure.assumptions:
        description_parts.append(f"Assumption: {assumption}")
    description = " ".join(
        p if p.rstrip().endswith((".", "!", "?")) else f"{p.rstrip()}."
        for p in description_parts if p and p.strip()
    ).strip()
    if description:
        for chunk in _wrap(description, 96):
            lines.append(f"{INDENT * depth}/// {chunk}")

    lines.append(f"{INDENT * depth}measure {quote_name(measure.name)} = ")
    # The expression sits one level deeper; a single-line one may share the line
    # but a block is safer and uniform.
    expression_lines = _lines(measure.expression.strip())
    if len(expression_lines) == 1:
        lines[-1] = (
            f"{INDENT * depth}measure {quote_name(measure.name)} = "
            f"{expression_lines[0]}"
        )
    else:
        lines[-1] = f"{INDENT * depth}measure {quote_name(measure.name)} = ```"
        for line in expression_lines:
            lines.append(f"{INDENT * (depth + 1)}{line}" if line.strip() else "")
        lines.append(f"{INDENT * (depth + 1)}```")

    if measure.format_string:
        lines.append(f"{INDENT * (depth + 1)}formatString: {measure.format_string}")
    if measure.display_folder:
        lines.append(f"{INDENT * (depth + 1)}displayFolder: {measure.display_folder}")
    if measure.is_hidden:
        lines.append(f"{INDENT * (depth + 1)}isHidden")
    lines.append("")
    lines.extend(_annotation("PBI_FormatHint", "{\"isGeneralNumber\":true}"
                             if not measure.format_string else "{}",
                             depth=depth + 1))
    lines.append("")
    return lines


def partition_tmdl(table: TableDefinition, *, depth: int = 1) -> list[str]:
    """The Power Query partition that loads the table."""
    expression = table.m_expression.strip() or (
        f"let\n    Source = {quote_name(table.dataset_name or table.name)}\nin\n    Source"
    )
    lines = [f"{INDENT * depth}partition {quote_name(table.name)} = m"]
    lines.append(f"{INDENT * (depth + 1)}mode: import")
    lines.extend(_expression("source", expression, depth=depth + 1))
    lines.append("")
    return lines


def table_tmdl(table: TableDefinition, model: SemanticModel) -> str:
    lines: list[str] = []
    description = " ".join(
        p for p in (table.description, table.grain, table.rationale) if p
    ).strip()
    if description:
        for chunk in _wrap(description, 96):
            lines.append(f"/// {chunk}")
    header = f"table {quote_name(table.name)}"
    lines.append(header)
    if table.is_hidden:
        lines.append(f"{INDENT}isHidden")
    lines.append(f"{INDENT}lineageTag: {_lineage_tag(table.name)}")
    lines.append("")

    for column in table.columns:
        lines.extend(column_tmdl(column, depth=1))

    measures = [m for m in model.measures if m.home_table == table.name]
    for measure in measures:
        lines.extend(measure_tmdl(measure, depth=1))

    lines.extend(partition_tmdl(table, depth=1))

    if table.role is TableRole.DATE:
        # dataCategory marks the table's intent; "mark as date table" itself is
        # a Desktop action and is called out in OPEN_IN_POWER_BI.md.
        lines.append(f"{INDENT}annotation PBI_ResultType = Table")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _lineage_tag(seed: str) -> str:
    """A stable, deterministic lineage tag.

    Power BI expects a GUID. Deriving it from the object name means
    regenerating the project produces identical files, so a diff shows only
    real changes -- which matters if the project is in source control.
    """
    import hashlib
    digest = hashlib.sha1(f"agent-analyst:{seed}".encode()).hexdigest()
    return (
        f"{digest[0:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
    )


def relationships_tmdl(model: SemanticModel) -> str:
    """All relationships, in one file as Power BI writes them."""
    lines: list[str] = []
    for relationship in model.relationships:
        if relationship.approved is False:
            lines.append(
                f"/// NOT CREATED: {relationship.key} was rejected during review."
            )
            lines.append("")
            continue
        notes = [relationship.rationale] + relationship.evidence[:3]
        for chunk in _wrap(" ".join(n for n in notes if n), 96):
            lines.append(f"/// {chunk}")
        lines.append(f"relationship {_lineage_tag(relationship.key)}")
        if not relationship.is_active:
            lines.append(f"{INDENT}isActive: false")
        if relationship.cardinality is Cardinality.MANY_TO_MANY:
            lines.append(f"{INDENT}fromCardinality: many")
            lines.append(f"{INDENT}toCardinality: many")
        elif relationship.cardinality is Cardinality.ONE_TO_ONE:
            lines.append(f"{INDENT}fromCardinality: one")
            lines.append(f"{INDENT}toCardinality: one")
        if relationship.cross_filter is CrossFilter.BOTH:
            lines.append(f"{INDENT}crossFilteringBehavior: bothDirections")
        # Never assert referential integrity when orphan keys were measured:
        # Power BI would then drop the unmatched rows from every query.
        if relationship.rely_on_referential_integrity and not relationship.orphan_key_count:
            lines.append(f"{INDENT}relyOnReferentialIntegrity")
        lines.append(
            f"{INDENT}fromColumn: {quote_name(relationship.from_table)}."
            f"{quote_name(relationship.from_column)}"
        )
        lines.append(
            f"{INDENT}toColumn: {quote_name(relationship.to_table)}."
            f"{quote_name(relationship.to_column)}"
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n" if lines else ""


def model_tmdl(model: SemanticModel, *, table_names: list[str]) -> str:
    lines = [
        "model Model",
        f"{INDENT}culture: {model.culture}",
        f"{INDENT}defaultPowerBIDataSourceVersion: powerBI_V3",
        f"{INDENT}discourageImplicitMeasures",
        f"{INDENT}sourceQueryCulture: {model.culture}",
        f"{INDENT}dataAccessOptions",
        f"{INDENT * 2}legacyRedirects",
        f"{INDENT * 2}returnErrorValuesAsNull",
        "",
        f"{INDENT}annotation PBI_QueryOrder = "
        + quote_string("[" + ", ".join(f'"{n}"' for n in table_names) + "]"),
        "",
        f"{INDENT}annotation PBI_ProTooling = " + quote_string('["DevMode"]'),
        "",
    ]
    for name in table_names:
        lines.append(f"ref table {quote_name(name)}")
    lines.append("")
    lines.append("ref cultureInfo en-US")
    return "\n".join(lines).rstrip() + "\n"


def database_tmdl(*, compatibility_level: int = 1550) -> str:
    return "\n".join([
        "database",
        f"{INDENT}compatibilityLevel: {compatibility_level}",
        "",
    ])


def expressions_tmdl(parameters: dict[str, str]) -> str:
    """Shared M expressions: the DataFolder parameter the queries read from.

    A parameter rather than a hard-coded path, so a developer can repoint the
    project at their own copy of the data without editing every query.
    """
    lines: list[str] = []
    for name, value in parameters.items():
        lines.append(
            f"/// Change this to point the project at your own copy of the data."
        )
        lines.append(f"expression {quote_name(name)} = {value} meta "
                     f"[IsParameterQuery=true, Type=\"Text\", IsParameterQueryRequired=true]")
        lines.append(f"{INDENT}lineageTag: {_lineage_tag('expr:' + name)}")
        lines.append("")
        lines.extend(_annotation("PBI_NavigationStepName", "Navigation", depth=1))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n" if lines else ""


def culture_tmdl(culture: str = "en-US") -> str:
    return "\n".join([
        f"cultureInfo {culture}",
        f"{INDENT}linguisticMetadata = ",
        f"{INDENT * 2}{{",
        f"{INDENT * 3}\"Version\": \"1.0.0\",",
        f"{INDENT * 3}\"Language\": \"{culture}\"",
        f"{INDENT * 2}}}",
        f"{INDENT * 2}contentType: json",
        "",
    ])


def _wrap(text: str, width: int) -> list[str]:
    words, out, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out or [""]
