"""Model-aware validation of a parsed DAX expression.

This is the gate the brief describes: LLM-generated code must not be treated
as valid merely because it looks plausible. Walking the AST against the
approved ``SemanticModel`` catches, with a position and a specific message:

* a table, column or measure that does not exist (with a 'did you mean');
* a function that is not DAX at all (``SUMPRODUCT``, ``VLOOKUP``) or does not
  exist (``TOTALYTDX``);
* the wrong number of arguments;
* a column used **bare** in a measure, outside any aggregation or row context
  -- the single most common DAX error, and one that produces a confusing
  engine error rather than a wrong number;
* time intelligence used without a date table in the model;
* a measure that references itself, directly or through a cycle;
* ``USERELATIONSHIP`` outside ``CALCULATE``;
* a relationship path that does not exist for ``RELATED``.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from ...schemas.common import Severity
from ...schemas.semantic import MeasureDefinition, SemanticModel, TableRole
from . import functions
from .lexer import DaxSyntaxError
from .parser import (
    BinaryOp, ColumnRef, Expr, FunctionCall, InOp, MeasureRef, TableRef, UnaryOp,
    VarBlock, VarRef, parse, walk,
)


@dataclass
class RefIssue:
    code: str
    severity: Severity
    message: str
    position: int = 0
    target: str = ""
    suggestion: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity in {Severity.CRITICAL, Severity.HIGH}


@dataclass
class RefResult:
    issues: list[RefIssue] = field(default_factory=list)
    referenced_columns: list[str] = field(default_factory=list)
    referenced_measures: list[str] = field(default_factory=list)
    referenced_tables: list[str] = field(default_factory=list)
    functions_used: list[str] = field(default_factory=list)
    uses_time_intelligence: bool = False
    creates_row_context: bool = False

    @property
    def ok(self) -> bool:
        return not any(i.blocking for i in self.issues)

    @property
    def blocking(self) -> list[RefIssue]:
        return [i for i in self.issues if i.blocking]


def _closest(name: str, candidates: list[str]) -> str:
    matches = difflib.get_close_matches(name.lower(), [c.lower() for c in candidates],
                                        n=1, cutoff=0.7)
    if not matches:
        return ""
    return next((c for c in candidates if c.lower() == matches[0]), "")


#: Functions inside which a bare column reference is legitimate because they
#: establish a row context, plus the aggregations that consume a column.
_ROW_CONTEXT_FUNCTIONS = {
    name for name, fn in functions.CATALOGUE.items()
    if fn.creates_row_context
}
_COLUMN_CONSUMING_FUNCTIONS = {
    name for name, fn in functions.CATALOGUE.items()
    if fn.column_args or fn.category in {
        functions.FnCategory.AGGREGATION, functions.FnCategory.STATISTICAL,
    }
}
#: Inside these, a bare column is a *filter predicate*, which is fine.
_FILTER_FUNCTIONS = {"CALCULATE", "CALCULATETABLE", "FILTER", "ALL", "ALLEXCEPT",
                     "ALLSELECTED", "REMOVEFILTERS", "VALUES", "DISTINCT",
                     "KEEPFILTERS", "TREATAS", "SUMMARIZE", "SUMMARIZECOLUMNS",
                     "GROUPBY", "SELECTCOLUMNS", "ADDCOLUMNS", "TOPN"}


def _annotate_context(node: Expr, inside: frozenset[str] = frozenset()) -> dict[int, frozenset[str]]:
    """Map each node's id to the set of enclosing function names.

    Lets us answer "is this column inside an aggregation or a row context?"
    without a second pass over the source text.
    """
    out: dict[int, frozenset[str]] = {id(node): inside}

    def recurse(n: Expr, ctx: frozenset[str]) -> None:
        out[id(n)] = ctx
        if isinstance(n, FunctionCall):
            child = ctx | {n.name.upper()}
            for arg in n.args:
                recurse(arg, child)
        elif isinstance(n, UnaryOp):
            recurse(n.operand, ctx)
        elif isinstance(n, BinaryOp):
            recurse(n.left, ctx)
            recurse(n.right, ctx)
        elif isinstance(n, InOp):
            recurse(n.left, ctx)
            for v in n.values:
                recurse(v, ctx)
            if n.table is not None:
                recurse(n.table, ctx)
        elif isinstance(n, VarBlock):
            for _, value in n.variables:
                recurse(value, ctx)
            recurse(n.body, ctx)

    recurse(node, inside)
    return out


def validate_references(
    expression: str, model: SemanticModel, *,
    measure_name: str = "", home_table: str = "",
) -> RefResult:
    """Resolve every name in ``expression`` against ``model``."""
    result = RefResult()
    try:
        tree = parse(expression)
    except DaxSyntaxError as exc:
        result.issues.append(RefIssue(
            code="dax.syntax", severity=Severity.CRITICAL,
            message=str(exc), position=getattr(exc, "position", 0),
        ))
        return result

    table_names = [t.name for t in model.tables]
    measure_names = [m.name for m in model.measures]
    contexts = _annotate_context(tree)
    var_names: set[str] = set()
    for node in walk(tree):
        if isinstance(node, VarBlock):
            var_names.update(name.lower() for name, _ in node.variables)

    # ---- functions ---------------------------------------------------
    for call in (n for n in walk(tree) if isinstance(n, FunctionCall)):
        upper = call.name.upper()
        result.functions_used.append(upper)

        wrong = functions.is_known_wrong(upper)
        if wrong:
            result.issues.append(RefIssue(
                code="dax.not_a_dax_function", severity=Severity.CRITICAL,
                message=f"'{call.name}' is not a DAX function. {wrong}",
                position=call.position, target=call.name,
                suggestion=functions.suggest(upper),
            ))
            continue

        fn = functions.lookup(upper)
        if fn is None:
            hint = functions.suggest(upper)
            result.issues.append(RefIssue(
                code="dax.unrecognised_function", severity=Severity.HIGH,
                message=f"'{call.name}' is not in the function catalogue"
                        + (f". Did you mean {hint}?" if hint else
                           ". If it is a genuine DAX function, it is outside the "
                           "catalogue this deployment can check, so it has NOT been "
                           "verified."),
                position=call.position, target=call.name, suggestion=hint,
            ))
            continue

        count = len(call.args)
        if count < fn.min_args or (fn.max_args is not None and count > fn.max_args):
            expected = (
                f"{fn.min_args}" if fn.max_args == fn.min_args
                else f"{fn.min_args}-{fn.max_args}" if fn.max_args is not None
                else f"at least {fn.min_args}"
            )
            result.issues.append(RefIssue(
                code="dax.argument_count", severity=Severity.CRITICAL,
                message=f"{upper} takes {expected} argument(s) but was given {count}."
                        + (f" {fn.note}" if fn.note else ""),
                position=call.position, target=call.name,
            ))

        if fn.requires_date_table:
            result.uses_time_intelligence = True
            if not model.has_date_dimension:
                result.issues.append(RefIssue(
                    code="dax.time_intelligence_without_date_table",
                    severity=Severity.CRITICAL,
                    message=f"{upper} needs a date table, but the model has no table with "
                            f"the 'date' role. Time intelligence will return wrong results "
                            f"or an error.",
                    position=call.position, target=call.name,
                ))
        if fn.creates_row_context:
            result.creates_row_context = True

        if upper == "USERELATIONSHIP":
            enclosing = contexts.get(id(call), frozenset())
            if "CALCULATE" not in enclosing and "CALCULATETABLE" not in enclosing:
                result.issues.append(RefIssue(
                    code="dax.userelationship_outside_calculate",
                    severity=Severity.CRITICAL,
                    message="USERELATIONSHIP is only valid as a CALCULATE modifier.",
                    position=call.position, target=call.name,
                ))

        warning = functions.USAGE_WARNINGS.get(upper)
        if warning:
            result.issues.append(RefIssue(
                code="dax.usage_note", severity=Severity.LOW,
                message=warning, position=call.position, target=call.name,
            ))

    # ---- tables ------------------------------------------------------
    for ref in (n for n in walk(tree) if isinstance(n, TableRef)):
        if ref.name.lower() in var_names:
            continue
        if model.table(ref.name) is None:
            hint = _closest(ref.name, table_names)
            result.issues.append(RefIssue(
                code="dax.unknown_table", severity=Severity.CRITICAL,
                message=f"There is no table named '{ref.name}' in the model."
                        + (f" Did you mean '{hint}'?" if hint else
                           f" Tables available: {', '.join(table_names) or 'none'}."),
                position=ref.position, target=ref.name, suggestion=hint,
            ))
        elif ref.name not in result.referenced_tables:
            result.referenced_tables.append(ref.name)

    # ---- columns and measures ----------------------------------------
    for ref in (n for n in walk(tree) if isinstance(n, ColumnRef)):
        if ref.table:
            table = model.table(ref.table)
            if table is None:
                hint = _closest(ref.table, table_names)
                result.issues.append(RefIssue(
                    code="dax.unknown_table", severity=Severity.CRITICAL,
                    message=f"There is no table named '{ref.table}' in the model."
                            + (f" Did you mean '{hint}'?" if hint else ""),
                    position=ref.position, target=str(ref), suggestion=hint,
                ))
                continue
            column = table.column(ref.column)
            if column is None:
                hint = _closest(ref.column, table.column_names)
                # A qualified reference to a measure is a common mistake.
                if model.measure(ref.column) is not None:
                    result.issues.append(RefIssue(
                        code="dax.measure_referenced_as_column",
                        severity=Severity.HIGH,
                        message=f"'{ref.column}' is a measure, not a column of "
                                f"'{ref.table}'. Reference a measure as [{ref.column}] "
                                f"without the table name.",
                        position=ref.position, target=str(ref),
                        suggestion=f"[{ref.column}]",
                    ))
                    continue
                result.issues.append(RefIssue(
                    code="dax.unknown_column", severity=Severity.CRITICAL,
                    message=f"'{ref.table}' has no column named '{ref.column}'."
                            + (f" Did you mean '{hint}'?" if hint else
                               f" Columns available: "
                               f"{', '.join(table.column_names[:12]) or 'none'}."),
                    position=ref.position, target=str(ref), suggestion=hint,
                ))
                continue
            qualified = f"{table.name}[{column.name}]"
            if qualified not in result.referenced_columns:
                result.referenced_columns.append(qualified)
            _check_bare_column(result, ref, contexts, qualified)
            continue

        # unqualified [Name]: a measure, or a column in a row context
        measure = model.measure(ref.column)
        if measure is not None:
            if ref.column not in result.referenced_measures:
                result.referenced_measures.append(ref.column)
            if measure_name and measure.name.lower() == measure_name.lower():
                result.issues.append(RefIssue(
                    code="dax.self_reference", severity=Severity.CRITICAL,
                    message=f"The measure '{measure_name}' references itself, which is a "
                            f"circular definition.",
                    position=ref.position, target=f"[{ref.column}]",
                ))
            continue

        # not a measure: look for a column of that name
        owners = [t for t in model.tables if t.column(ref.column) is not None]
        if owners:
            table = (
                next((t for t in owners if t.name.lower() == home_table.lower()), None)
                or owners[0]
            )
            qualified = f"{table.name}[{table.column(ref.column).name}]"  # type: ignore[union-attr]
            if qualified not in result.referenced_columns:
                result.referenced_columns.append(qualified)
            result.issues.append(RefIssue(
                code="dax.unqualified_column", severity=Severity.MEDIUM,
                message=f"[{ref.column}] is a column of '{table.name}', not a measure. "
                        f"In a measure body a column must be written as "
                        f"{table.name}[{ref.column}]"
                        + (f" (it also exists in "
                           f"{', '.join(t.name for t in owners[1:])}, so the unqualified "
                           f"form is ambiguous)" if len(owners) > 1 else "")
                        + ".",
                position=ref.position, target=f"[{ref.column}]",
                suggestion=f"{table.name}[{ref.column}]",
            ))
            _check_bare_column(result, ref, contexts, qualified)
            continue

        hint = _closest(ref.column, measure_names + [
            c for t in model.tables for c in t.column_names
        ])
        result.issues.append(RefIssue(
            code="dax.unknown_measure", severity=Severity.CRITICAL,
            message=f"'[{ref.column}]' is neither a measure nor a column in the model."
                    + (f" Did you mean '{hint}'?" if hint else
                       f" Measures available: {', '.join(measure_names[:10]) or 'none'}."),
            position=ref.position, target=f"[{ref.column}]", suggestion=hint,
        ))

    # ---- variables ---------------------------------------------------
    for ref in (n for n in walk(tree) if isinstance(n, VarRef)):
        if ref.name.lower() not in var_names:
            result.issues.append(RefIssue(
                code="dax.unknown_identifier", severity=Severity.CRITICAL,
                message=f"'{ref.name}' is not a defined variable, table or function.",
                position=ref.position, target=ref.name,
            ))

    # ---- RELATED needs a relationship path ---------------------------
    for call in (n for n in walk(tree) if isinstance(n, FunctionCall)):
        if call.name.upper() != "RELATED" or not call.args:
            continue
        arg = call.args[0]
        if not isinstance(arg, ColumnRef) or not arg.table:
            continue
        if not home_table:
            continue
        if arg.table.lower() == home_table.lower():
            result.issues.append(RefIssue(
                code="dax.related_same_table", severity=Severity.HIGH,
                message=f"RELATED({arg}) refers to the measure's own table; RELATED "
                        f"follows a relationship to a *different* table.",
                position=call.position, target=str(arg),
            ))
            continue
        paths = model.relationship_path(home_table, arg.table)
        if not paths:
            result.issues.append(RefIssue(
                code="dax.related_no_relationship", severity=Severity.CRITICAL,
                message=f"RELATED({arg}) requires a relationship path from "
                        f"'{home_table}' to '{arg.table}', and the model has none.",
                position=call.position, target=str(arg),
            ))
        elif len(paths) > 1:
            result.issues.append(RefIssue(
                code="dax.ambiguous_relationship_path", severity=Severity.HIGH,
                message=f"There are {len(paths)} relationship paths from '{home_table}' "
                        f"to '{arg.table}', so RELATED is ambiguous. Deactivate a "
                        f"relationship or use USERELATIONSHIP to choose one.",
                position=call.position, target=str(arg),
            ))

    return result


def _check_bare_column(
    result: RefResult, ref: ColumnRef, contexts: dict[int, frozenset[str]], qualified: str
) -> None:
    """Flag a column used with no aggregation and no row context.

    ``Measure = FactCost[cost_amount]`` is the classic error: it is not a
    scalar, so the engine either errors or silently takes an arbitrary value.
    """
    enclosing = contexts.get(id(ref), frozenset())
    if not enclosing:
        result.issues.append(RefIssue(
            code="dax.bare_column_in_measure", severity=Severity.CRITICAL,
            message=f"{qualified} is used with no aggregation and no row context. A "
                    f"measure must return a single value -- wrap it in SUM, AVERAGE, "
                    f"MAX, SELECTEDVALUE or an iterator.",
            position=ref.position, target=qualified,
            suggestion=f"SUM({qualified})",
        ))
        return
    safe = (
        enclosing & _COLUMN_CONSUMING_FUNCTIONS
        or enclosing & _ROW_CONTEXT_FUNCTIONS
        or enclosing & _FILTER_FUNCTIONS
    )
    if not safe:
        result.issues.append(RefIssue(
            code="dax.bare_column_in_measure", severity=Severity.HIGH,
            message=f"{qualified} appears inside {', '.join(sorted(enclosing))} with no "
                    f"aggregation and no row context, so it does not resolve to a single "
                    f"value.",
            position=ref.position, target=qualified,
            suggestion=f"SUM({qualified})",
        ))


def measure_dependencies(model: SemanticModel) -> dict[str, set[str]]:
    """measure name -> the measures it depends on."""
    out: dict[str, set[str]] = {}
    for measure in model.measures:
        deps: set[str] = set()
        try:
            tree = parse(measure.expression)
        except DaxSyntaxError:
            out[measure.name] = deps
            continue
        for ref in (n for n in walk(tree) if isinstance(n, ColumnRef)):
            if not ref.table and model.measure(ref.column) is not None:
                deps.add(model.measure(ref.column).name)  # type: ignore[union-attr]
        out[measure.name] = deps
    return out


def find_dependency_cycles(model: SemanticModel) -> list[list[str]]:
    """Measure cycles, e.g. A -> B -> A. Power BI refuses to load these."""
    graph = measure_dependencies(model)
    cycles: list[list[str]] = []
    seen_cycles: set[frozenset[str]] = set()

    def visit(node: str, stack: list[str], visited: set[str]) -> None:
        if node in stack:
            cycle = stack[stack.index(node):] + [node]
            key = frozenset(cycle)
            if key not in seen_cycles:
                seen_cycles.add(key)
                cycles.append(cycle)
            return
        if node in visited:
            return
        visited.add(node)
        for dep in sorted(graph.get(node, set())):
            visit(dep, stack + [node], visited)

    for name in sorted(graph):
        visit(name, [], set())
    return cycles


def topological_measure_order(model: SemanticModel) -> list[str]:
    """Measures ordered so dependencies come first. Used by the evaluator."""
    graph = measure_dependencies(model)
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(name: str) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            return  # cycle; reported separately
        state[name] = 1
        for dep in sorted(graph.get(name, set())):
            if dep in graph:
                visit(dep)
        state[name] = 2
        order.append(name)

    for name in sorted(graph):
        visit(name)
    return order
