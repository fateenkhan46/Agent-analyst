"""The DAX engine: lexer, parser, reference validation, evaluation."""
from __future__ import annotations

import pytest

from app.schemas.common import CanonicalType, Confidence, Severity
from app.schemas.semantic import (
    Cardinality, ColumnDefinition, MeasureDefinition, RelationshipDefinition,
    SemanticModel, TableDefinition, TableRole,
)
from app.services.dax.lexer import DaxSyntaxError, TokenKind, tokenise
from app.services.dax.parser import parse, to_source
from app.services.dax.references import (
    find_dependency_cycles, topological_measure_order, validate_references,
)


@pytest.fixture
def model() -> SemanticModel:
    return SemanticModel(
        project_id="p",
        tables=[
            TableDefinition(name="FactCost", role=TableRole.FACT, columns=[
                ColumnDefinition(name="cost_amount", data_type=CanonicalType.CURRENCY),
                ColumnDefinition(name="project_id", data_type=CanonicalType.IDENTIFIER),
                ColumnDefinition(name="transaction_date", data_type=CanonicalType.DATE),
            ]),
            TableDefinition(name="DimProject", role=TableRole.DIMENSION, columns=[
                ColumnDefinition(name="project_id", data_type=CanonicalType.IDENTIFIER,
                                 is_key=True),
                ColumnDefinition(name="approved_budget", data_type=CanonicalType.CURRENCY),
                ColumnDefinition(name="status", data_type=CanonicalType.STRING),
            ]),
            TableDefinition(name="DimDate", role=TableRole.DATE, columns=[
                ColumnDefinition(name="date", data_type=CanonicalType.DATE, is_key=True),
            ]),
        ],
        relationships=[
            RelationshipDefinition(
                from_table="FactCost", from_column="project_id",
                to_table="DimProject", to_column="project_id", confidence=Confidence.HIGH),
            RelationshipDefinition(
                from_table="FactCost", from_column="transaction_date",
                to_table="DimDate", to_column="date", confidence=Confidence.HIGH),
        ],
        measures=[
            MeasureDefinition(name="Total Cost", expression="SUM(FactCost[cost_amount])",
                              home_table="FactCost"),
            MeasureDefinition(name="Total Budget",
                              expression="SUM(DimProject[approved_budget])",
                              home_table="DimProject"),
        ],
    )


@pytest.fixture
def no_date_model(model: SemanticModel) -> SemanticModel:
    return model.model_copy(update={
        "tables": [t for t in model.tables if t.role is not TableRole.DATE],
        "relationships": [r for r in model.relationships if r.to_table != "DimDate"],
    })


# --- lexer ---------------------------------------------------------------
def test_double_minus_is_negation_not_a_comment() -> None:
    """DAX has no '--' comment; treating it as one would silently truncate."""
    kinds = [t.kind for t in tokenise("1 -- 2")]
    assert kinds.count(TokenKind.OPERATOR) == 2
    assert kinds[-2] is TokenKind.NUMBER


def test_doubled_quote_escapes_inside_a_string() -> None:
    tokens = tokenise('"he said ""hi"""')
    assert tokens[0].value == 'he said "hi"'


def test_comments_are_stripped() -> None:
    assert [t.value for t in tokenise("// gone\nSUM([x])")][0] == "SUM"
    assert [t.value for t in tokenise("/* gone */ 1")][0] == "1"


@pytest.mark.parametrize(
    "source", ["SUM([x", '"unterminated', "'unterminated", "[]", "SUM(@)", "/* open"]
)
def test_lexer_rejects_malformed_input(source: str) -> None:
    with pytest.raises(DaxSyntaxError):
        tokenise(source)


# --- parser --------------------------------------------------------------
@pytest.mark.parametrize("source", [
    "SUM(FactCost[cost_amount])",
    "CALCULATE(SUM(FactCost[cost_amount]), DimProject[status] = \"Active\")",
    "DIVIDE([Total Budget] - [Total Cost], [Total Budget])",
    "VAR b = [Total Budget] VAR c = [Total Cost] RETURN DIVIDE(b - c, b)",
    "SUMX(FactCost, FactCost[cost_amount] * 2)",
    "IF(ISBLANK([Total Budget]), BLANK(), 1)",
    "DimProject[status] IN {\"Active\", \"Closed\"}",
    "1 + 2 * 3 ^ 2",
    '"a" & "b"',
    "TOTALYTD([Total Cost], DimDate[date])",
])
def test_parser_accepts_valid_dax(source: str) -> None:
    assert parse(source) is not None


@pytest.mark.parametrize("source", [
    "SUM(", "SUM(T[a]", "SUM T[a])", "+", "VAR x = 1", "RETURN 1", "1 +",
    "SUM(,)", "SUM(T[a]) extra", "()", "", "   ",
    "DEFINE MEASURE T[m] = 1 EVALUATE T",
])
def test_parser_rejects_malformed_dax(source: str) -> None:
    with pytest.raises(DaxSyntaxError):
        parse(source)


def test_parser_roundtrips() -> None:
    source = "DIVIDE([Total Budget] - [Total Cost], [Total Budget])"
    assert to_source(parse(source)) == source


def test_operator_precedence() -> None:
    tree = parse("1 + 2 * 3")
    assert to_source(tree) == "1 + 2 * 3"
    # multiplication binds tighter, so the top node is the addition
    assert tree.op == "+"  # type: ignore[union-attr]


# --- reference validation ------------------------------------------------
def test_valid_measure_passes(model: SemanticModel) -> None:
    result = validate_references("SUM(FactCost[cost_amount])", model)
    assert result.ok, [i.message for i in result.issues]
    assert result.referenced_columns == ["FactCost[cost_amount]"]


def test_bare_column_in_a_measure_is_rejected(model: SemanticModel) -> None:
    """The commonest DAX error: a column is not a scalar."""
    result = validate_references("FactCost[cost_amount]", model)
    assert not result.ok
    codes = {i.code for i in result.issues}
    assert "dax.bare_column_in_measure" in codes


def test_bare_column_in_arithmetic_is_rejected(model: SemanticModel) -> None:
    result = validate_references("FactCost[cost_amount] + 1", model)
    assert not result.ok


def test_column_inside_an_iterator_is_fine(model: SemanticModel) -> None:
    result = validate_references("SUMX(FactCost, FactCost[cost_amount] * 2)", model)
    assert result.ok, [i.message for i in result.issues]
    assert result.creates_row_context


@pytest.mark.parametrize("source,expected_code", [
    ("SUM(FactCost[nope])", "dax.unknown_column"),
    ("SUM(NoTable[x])", "dax.unknown_table"),
    ("[No Such Measure]", "dax.unknown_measure"),
    ("SUMPRODUCT(FactCost[cost_amount], 2)", "dax.not_a_dax_function"),
    ("VLOOKUP(1, 2, 3)", "dax.not_a_dax_function"),
    ("TOTALYTDX([Total Cost], DimDate[date])", "dax.not_a_dax_function"),
    ("DIVIDE([Total Cost])", "dax.argument_count"),
    ("SUM(FactCost[cost_amount], 2)", "dax.argument_count"),
    ("FactCost[Total Cost]", "dax.measure_referenced_as_column"),
    ("USERELATIONSHIP(FactCost[transaction_date], DimDate[date])",
     "dax.userelationship_outside_calculate"),
])
def test_reference_errors_are_caught(
    model: SemanticModel, source: str, expected_code: str
) -> None:
    result = validate_references(source, model, home_table="FactCost")
    assert expected_code in {i.code for i in result.issues}, \
        [(i.code, i.message) for i in result.issues]


def test_typos_get_a_suggestion(model: SemanticModel) -> None:
    result = validate_references("SUM(FactCost[cost_amountt])", model)
    issue = next(i for i in result.issues if i.code == "dax.unknown_column")
    assert issue.suggestion == "cost_amount"


def test_time_intelligence_without_a_date_table_is_rejected(
    no_date_model: SemanticModel
) -> None:
    result = validate_references(
        "TOTALYTD([Total Cost], FactCost[transaction_date])", no_date_model
    )
    assert "dax.time_intelligence_without_date_table" in {i.code for i in result.issues}


def test_time_intelligence_date_column_is_not_a_bare_column(model: SemanticModel) -> None:
    """A regression guard: flagging the date argument as a bare column
    rejected every valid time-intelligence measure."""
    result = validate_references("TOTALYTD([Total Cost], DimDate[date])", model)
    assert result.ok, [i.message for i in result.issues]
    assert result.uses_time_intelligence


def test_self_reference_is_rejected(model: SemanticModel) -> None:
    result = validate_references(
        "[Total Cost] + 1", model, measure_name="Total Cost"
    )
    assert "dax.self_reference" in {i.code for i in result.issues}


def test_related_without_a_relationship_is_rejected(model: SemanticModel) -> None:
    stripped = model.model_copy(update={"relationships": []})
    result = validate_references(
        "SUMX(FactCost, RELATED(DimProject[approved_budget]))", stripped,
        home_table="FactCost",
    )
    assert "dax.related_no_relationship" in {i.code for i in result.issues}


def test_related_with_a_relationship_passes(model: SemanticModel) -> None:
    result = validate_references(
        "SUMX(FactCost, RELATED(DimProject[approved_budget]))", model,
        home_table="FactCost",
    )
    assert result.ok, [i.message for i in result.issues]


def test_measure_cycles_are_detected(model: SemanticModel) -> None:
    looped = model.model_copy(update={"measures": [
        MeasureDefinition(name="A", expression="[B] + 1"),
        MeasureDefinition(name="B", expression="[A] + 1"),
    ]})
    cycles = find_dependency_cycles(looped)
    assert cycles, "A -> B -> A must be reported"


def test_topological_order_puts_dependencies_first(model: SemanticModel) -> None:
    ordered = model.model_copy(update={"measures": model.measures + [
        MeasureDefinition(name="Variance", expression="[Total Budget] - [Total Cost]"),
        MeasureDefinition(name="Variance %", expression="DIVIDE([Variance], [Total Budget])"),
    ]})
    order = topological_measure_order(ordered)
    assert order.index("Total Cost") < order.index("Variance")
    assert order.index("Variance") < order.index("Variance %")


# --- filter path direction ----------------------------------------------
def test_galaxy_schema_is_not_reported_as_ambiguous(model: SemanticModel) -> None:
    """Two facts sharing a dimension is not ambiguity; conflating the lookup
    and filter directions made it look like it was."""
    galaxy = model.model_copy(update={
        "tables": model.tables + [
            TableDefinition(name="FactLabour", role=TableRole.FACT, columns=[
                ColumnDefinition(name="project_id", data_type=CanonicalType.IDENTIFIER),
                ColumnDefinition(name="hours", data_type=CanonicalType.DECIMAL),
            ]),
        ],
        "relationships": model.relationships + [
            RelationshipDefinition(
                from_table="FactLabour", from_column="project_id",
                to_table="DimProject", to_column="project_id",
                confidence=Confidence.HIGH),
        ],
    })
    assert len(galaxy.filter_paths("DimProject", "FactCost")) == 1
    assert len(galaxy.filter_paths("DimProject", "FactLabour")) == 1
    assert galaxy.filter_paths("FactCost", "FactLabour") == []


def test_two_date_columns_into_one_calendar_is_ambiguous(model: SemanticModel) -> None:
    ambiguous = model.model_copy(update={
        "relationships": model.relationships + [
            RelationshipDefinition(
                from_table="FactCost", from_column="project_id",
                to_table="DimDate", to_column="date", confidence=Confidence.HIGH),
        ],
    })
    assert len(ambiguous.filter_paths("DimDate", "FactCost")) == 2
