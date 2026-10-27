"""Evaluating a DAX measure against the real prepared data, via DuckDB.

There is no Analysis Services engine on Linux, so this is how a measure gets
an **actual number** that QA can compare against an independently-computed
expectation. It is honest about its limits: anything outside the documented
subset returns ``supported=False`` with the construct that was not handled,
and is never reported as a pass.

Design: a tree-walking interpreter whose *leaves* are SQL aggregate queries.

That matters for correctness, not just simplicity. Consider
``DIVIDE([Total Budget] - [Total Cost], [Total Budget])`` where budget lives on
the project dimension and cost on the fact table. Transpiling the whole
expression into one SQL statement over a join would multiply each project's
budget by its number of cost rows -- the exact double-counting error this
system is supposed to catch. DAX does not work that way: each measure is
evaluated independently in the current filter context and the arithmetic then
happens on scalars. So that is what this does: each aggregation becomes its own
``SELECT`` against its own base table (joined only where a *filter* requires
it), and the combining arithmetic runs in Python.

Supported subset: see docs/DAX_SUBSET.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import Field
from sqlalchemy.orm import Session

from ...schemas.common import Contract
from ...schemas.semantic import (
    Cardinality, MeasureDefinition, SemanticModel, TableDefinition, TableRole,
)
from .lexer import DaxSyntaxError
from .parser import (
    BinaryOp, BlankLit, BoolLit, ColumnRef, Expr, FunctionCall, InOp, NumberLit,
    StringLit, TableRef, UnaryOp, VarBlock, VarRef, parse, to_source,
)

#: Aggregations we can translate directly to SQL.
SQL_AGGREGATIONS: dict[str, str] = {
    "SUM": "SUM", "AVERAGE": "AVG", "MIN": "MIN", "MAX": "MAX",
    "COUNT": "COUNT", "COUNTA": "COUNT", "MEDIAN": "MEDIAN",
    "DISTINCTCOUNT": "COUNT(DISTINCT {col})",
    "DISTINCTCOUNTNOBLANK": "COUNT(DISTINCT {col})",
    "STDEV.P": "STDDEV_POP", "STDEV.S": "STDDEV_SAMP",
    "VAR.P": "VAR_POP", "VAR.S": "VAR_SAMP",
}

#: Scalar functions evaluated in Python once their arguments are scalars.
SCALAR_FUNCTIONS = {
    "DIVIDE", "IF", "IFERROR", "ABS", "ROUND", "INT", "SQRT", "POWER",
    "BLANK", "ISBLANK", "COALESCE", "SIGN", "MOD", "TRUNC", "MAX", "MIN",
    "SWITCH", "NOT",
}

#: Time-intelligence functions handled as filter modifiers.
TIME_MODIFIERS = {
    "PREVIOUSMONTH", "PREVIOUSYEAR", "PREVIOUSQUARTER", "PREVIOUSDAY",
    "SAMEPERIODLASTYEAR", "DATESYTD", "DATESMTD", "DATESQTD", "NEXTMONTH",
    "NEXTYEAR", "PARALLELPERIOD", "DATEADD",
}


class Unsupported(Exception):
    """A construct outside the supported subset. Carries what was hit."""

    def __init__(self, construct: str, detail: str = "") -> None:
        super().__init__(f"{construct}: {detail}" if detail else construct)
        self.construct = construct
        self.detail = detail


class EvaluationFailed(Exception):
    """The SQL ran but something was wrong (missing dataset, bad type)."""


class EvaluationOutcome(Contract):
    """What an evaluation produced. ``supported=False`` is a first-class
    result, not an error -- it means 'we did not check this', and QA records
    it as ``not_supported`` rather than as a pass."""

    measure: str
    supported: bool = True
    value: float | None = None
    is_blank: bool = False
    engine: str = "duckdb"
    sql_executed: list[str] = Field(default_factory=list)
    unsupported_construct: str = ""
    reason: str = ""
    filters_applied: dict[str, str] = Field(default_factory=dict)
    row_counts: dict[str, int] = Field(default_factory=dict)
    duration_ms: int = 0
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Filter context
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Predicate:
    table: str
    column: str
    op: str                       # = <> < > <= >= in between
    values: tuple[Any, ...] = ()

    def describe(self) -> str:
        if self.op == "in":
            return f"{self.table}[{self.column}] IN ({', '.join(map(str, self.values))})"
        if self.op == "between":
            return f"{self.table}[{self.column}] BETWEEN {self.values[0]} AND {self.values[1]}"
        return f"{self.table}[{self.column}] {self.op} {self.values[0] if self.values else ''}"


@dataclass
class FilterContext:
    predicates: tuple[Predicate, ...] = ()
    #: True when ALL/REMOVEFILTERS cleared everything.
    cleared_tables: frozenset[str] = frozenset()

    def with_predicates(self, extra: list[Predicate]) -> "FilterContext":
        return FilterContext(
            predicates=self.predicates + tuple(extra),
            cleared_tables=self.cleared_tables,
        )

    def cleared(self, tables: list[str]) -> "FilterContext":
        return FilterContext(
            predicates=tuple(
                p for p in self.predicates if p.table not in {t for t in tables}
            ),
            cleared_tables=self.cleared_tables | frozenset(tables),
        )

    def for_table(self, table: str) -> list[Predicate]:
        return [p for p in self.predicates if p.table == table]

    def describe(self) -> dict[str, str]:
        return {
            f"{p.table}[{p.column}]": p.describe() for p in self.predicates
        }


# --------------------------------------------------------------------------
# The evaluator
# --------------------------------------------------------------------------
def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).replace("'", "''")
    return f"'{text}'"


class DaxEvaluator:
    """Evaluates measures of the supported subset against DuckDB."""

    def __init__(
        self, connection: Any, model: SemanticModel, *,
        table_sql_names: dict[str, str],
    ) -> None:
        self.con = connection
        self.model = model
        #: model table name -> DuckDB relation name
        self.sql_names = table_sql_names
        self.sql_log: list[str] = []
        self.notes: list[str] = []
        self._measure_stack: list[str] = []

    # -- public --------------------------------------------------------
    def evaluate_measure(
        self, measure: MeasureDefinition, *, filters: FilterContext | None = None
    ) -> float | None:
        if measure.name.lower() in {m.lower() for m in self._measure_stack}:
            raise Unsupported(
                "circular measure reference",
                " -> ".join(self._measure_stack + [measure.name]),
            )
        self._measure_stack.append(measure.name)
        try:
            tree = parse(measure.expression)
            return self._eval(tree, filters or FilterContext(), home=measure.home_table)
        finally:
            self._measure_stack.pop()

    # -- the interpreter ------------------------------------------------
    def _eval(self, node: Expr, ctx: FilterContext, *, home: str = "",
              variables: dict[str, Any] | None = None) -> Any:
        variables = variables or {}

        if isinstance(node, NumberLit):
            return node.value
        if isinstance(node, StringLit):
            return node.value
        if isinstance(node, BoolLit):
            return node.value
        if isinstance(node, BlankLit):
            return None

        if isinstance(node, VarRef):
            if node.name.lower() not in variables:
                raise Unsupported("undefined variable", node.name)
            return variables[node.name.lower()]

        if isinstance(node, VarBlock):
            scope = dict(variables)
            for name, value_expr in node.variables:
                scope[name.lower()] = self._eval(
                    value_expr, ctx, home=home, variables=scope
                )
            return self._eval(node.body, ctx, home=home, variables=scope)

        if isinstance(node, UnaryOp):
            if node.op == "NOT":
                operand = self._eval(node.operand, ctx, home=home, variables=variables)
                return not bool(operand)
            value = self._eval(node.operand, ctx, home=home, variables=variables)
            if value is None:
                return None
            return -value if node.op == "-" else value

        if isinstance(node, BinaryOp):
            return self._eval_binary(node, ctx, home=home, variables=variables)

        if isinstance(node, ColumnRef):
            # A measure reference, or a bare column (invalid in a measure --
            # the reference validator reports that; here it is unsupported).
            if not node.table:
                measure = self.model.measure(node.column)
                if measure is None:
                    raise Unsupported("unresolved reference", f"[{node.column}]")
                return self.evaluate_measure(measure, filters=ctx)
            raise Unsupported(
                "bare column reference",
                f"{node.table}[{node.column}] is not aggregated",
            )

        if isinstance(node, TableRef):
            raise Unsupported("bare table reference", node.name)

        if isinstance(node, InOp):
            raise Unsupported("IN outside a filter argument", to_source(node))

        if isinstance(node, FunctionCall):
            return self._eval_call(node, ctx, home=home, variables=variables)

        raise Unsupported("expression form", type(node).__name__)

    def _eval_binary(
        self, node: BinaryOp, ctx: FilterContext, *, home: str, variables: dict[str, Any]
    ) -> Any:
        left = self._eval(node.left, ctx, home=home, variables=variables)
        right = self._eval(node.right, ctx, home=home, variables=variables)

        if node.op in {"&&", "AND"}:
            return bool(left) and bool(right)
        if node.op in {"||", "OR"}:
            return bool(left) or bool(right)
        if node.op == "&":
            return f"{'' if left is None else left}{'' if right is None else right}"
        if node.op in {"=", "<>", "<", ">", "<=", ">="}:
            if left is None or right is None:
                # DAX compares BLANK as 0 for numbers; keep that behaviour.
                left = 0 if left is None and isinstance(right, (int, float)) else left
                right = 0 if right is None and isinstance(left, (int, float)) else right
            if left is None or right is None:
                return node.op in {"=", "<=", ">="} and left == right
            try:
                return {
                    "=": left == right, "<>": left != right, "<": left < right,
                    ">": left > right, "<=": left <= right, ">=": left >= right,
                }[node.op]
            except TypeError:
                return {"=": False, "<>": True}.get(node.op, False)

        # arithmetic: BLANK behaves as 0 for + - * and makes / undefined
        if node.op in {"+", "-", "*", "/", "^"}:
            a = 0.0 if left is None else float(left)
            b = 0.0 if right is None else float(right)
            if node.op == "+":
                return a + b
            if node.op == "-":
                return a - b
            if node.op == "*":
                return a * b
            if node.op == "^":
                return a ** b
            if b == 0:
                # Bare division by zero is an error in DAX; DIVIDE is the safe form.
                raise EvaluationFailed(
                    "division by zero. Use DIVIDE(numerator, denominator) so a zero or "
                    "missing denominator returns BLANK instead of an error."
                )
            return a / b
        raise Unsupported("operator", node.op)

    def _eval_call(
        self, node: FunctionCall, ctx: FilterContext, *, home: str,
        variables: dict[str, Any],
    ) -> Any:
        name = node.name.upper()

        # --- aggregations over a column --------------------------------
        if name in SQL_AGGREGATIONS and len(node.args) == 1 and \
                isinstance(node.args[0], ColumnRef) and node.args[0].table:
            return self._aggregate(name, node.args[0], ctx)

        if name == "COUNTROWS":
            return self._count_rows(node, ctx, home=home)

        # --- CALCULATE and friends -------------------------------------
        if name in {"CALCULATE", "CALCULATETABLE"}:
            if not node.args:
                raise Unsupported("CALCULATE with no expression")
            new_ctx = ctx
            for modifier in node.args[1:]:
                new_ctx = self._apply_modifier(modifier, new_ctx, home=home)
            return self._eval(node.args[0], new_ctx, home=home, variables=variables)

        if name == "KEEPFILTERS" and len(node.args) == 1:
            return self._eval(node.args[0], ctx, home=home, variables=variables)

        # --- scalar functions ------------------------------------------
        if name == "DIVIDE":
            numerator = self._eval(node.args[0], ctx, home=home, variables=variables)
            denominator = self._eval(node.args[1], ctx, home=home, variables=variables)
            alternate = (
                self._eval(node.args[2], ctx, home=home, variables=variables)
                if len(node.args) > 2 else None
            )
            if denominator in (None, 0, 0.0):
                return alternate
            return float(0 if numerator is None else numerator) / float(denominator)

        if name in {"IF", "IFERROR"}:
            if name == "IFERROR":
                try:
                    value = self._eval(node.args[0], ctx, home=home, variables=variables)
                    return value
                except (EvaluationFailed, ZeroDivisionError):
                    return self._eval(node.args[1], ctx, home=home, variables=variables)
            condition = self._eval(node.args[0], ctx, home=home, variables=variables)
            if condition:
                return self._eval(node.args[1], ctx, home=home, variables=variables)
            if len(node.args) > 2:
                return self._eval(node.args[2], ctx, home=home, variables=variables)
            return None

        if name == "SWITCH":
            subject = self._eval(node.args[0], ctx, home=home, variables=variables)
            rest = node.args[1:]
            i = 0
            while i + 1 < len(rest):
                candidate = self._eval(rest[i], ctx, home=home, variables=variables)
                if (subject is True and candidate) or subject == candidate:
                    return self._eval(rest[i + 1], ctx, home=home, variables=variables)
                i += 2
            if len(rest) % 2 == 1:
                return self._eval(rest[-1], ctx, home=home, variables=variables)
            return None

        if name == "BLANK":
            return None
        if name == "ISBLANK":
            return self._eval(node.args[0], ctx, home=home, variables=variables) is None
        if name == "COALESCE":
            for arg in node.args:
                value = self._eval(arg, ctx, home=home, variables=variables)
                if value is not None:
                    return value
            return None
        if name == "NOT":
            return not bool(self._eval(node.args[0], ctx, home=home, variables=variables))

        if name in {"ABS", "ROUND", "INT", "SQRT", "POWER", "SIGN", "MOD", "TRUNC"}:
            args = [
                self._eval(a, ctx, home=home, variables=variables) for a in node.args
            ]
            numbers = [0.0 if a is None else float(a) for a in args]
            import math
            if name == "ABS":
                return abs(numbers[0])
            if name == "ROUND":
                return round(numbers[0], int(numbers[1]) if len(numbers) > 1 else 0)
            if name in {"INT", "TRUNC"}:
                return float(math.trunc(numbers[0]))
            if name == "SQRT":
                return math.sqrt(numbers[0]) if numbers[0] >= 0 else None
            if name == "POWER":
                return numbers[0] ** numbers[1]
            if name == "SIGN":
                return float((numbers[0] > 0) - (numbers[0] < 0))
            if name == "MOD":
                return numbers[0] % numbers[1] if numbers[1] else None

        if name in {"MAX", "MIN"} and len(node.args) == 2:
            values = [
                self._eval(a, ctx, home=home, variables=variables) for a in node.args
            ]
            numbers = [v for v in values if v is not None]
            if not numbers:
                return None
            return max(numbers) if name == "MAX" else min(numbers)

        if name == "SELECTEDVALUE" and node.args and isinstance(node.args[0], ColumnRef):
            return self._selected_value(node, ctx)

        # --- time intelligence as a wrapper ----------------------------
        if name in {"TOTALYTD", "TOTALQTD", "TOTALMTD"}:
            return self._total_to_date(name, node, ctx, home=home, variables=variables)

        raise Unsupported(
            "function", f"{name} is outside the evaluable subset "
                        f"(see docs/DAX_SUBSET.md)"
        )

    # -- SQL leaves ----------------------------------------------------
    def _resolve_table(self, name: str) -> TableDefinition:
        table = self.model.table(name)
        if table is None:
            raise EvaluationFailed(f"table '{name}' is not in the model")
        if table.name not in self.sql_names:
            raise Unsupported(
                "missing dataset", f"no data is loaded for table '{table.name}'"
            )
        return table

    def _join_clause(self, base: TableDefinition, needed: set[str]) -> tuple[str, list[str]]:
        """Build the LEFT JOINs that reach every filtered dimension.

        Only relationships *from* the base table are followed, one hop, which
        is the star-schema case. A filter that needs more than one hop is
        reported as unsupported rather than approximated.
        """
        clauses: list[str] = []
        notes: list[str] = []
        reached = {base.name}
        for target in sorted(needed - {base.name}):
            relationship = next(
                (
                    r for r in self.model.active_relationships()
                    if r.from_table == base.name and r.to_table == target
                ),
                None,
            )
            if relationship is None:
                # try the reverse direction (dimension -> fact), still one hop
                relationship = next(
                    (
                        r for r in self.model.active_relationships()
                        if r.to_table == base.name and r.from_table == target
                    ),
                    None,
                )
                if relationship is None:
                    raise Unsupported(
                        "unreachable filter",
                        f"no single relationship connects '{base.name}' to '{target}', "
                        f"so a filter on '{target}' cannot be applied",
                    )
                left_col, right_col = relationship.to_column, relationship.from_column
            else:
                left_col, right_col = relationship.from_column, relationship.to_column
            if target not in self.sql_names:
                raise Unsupported("missing dataset", f"no data loaded for '{target}'")
            clauses.append(
                f"LEFT JOIN {_quote(self.sql_names[target])} AS {_quote(target)} "
                f"ON {_quote(base.name)}.{_quote(left_col)} = "
                f"{_quote(target)}.{_quote(right_col)}"
            )
            reached.add(target)
            notes.append(f"joined {target} via {left_col} = {right_col}")
        return " ".join(clauses), notes

    def _where_clause(self, predicates: list[Predicate]) -> str:
        if not predicates:
            return ""
        parts: list[str] = []
        for p in predicates:
            column = f"{_quote(p.table)}.{_quote(p.column)}"
            if p.op == "in":
                values = ", ".join(_literal(v) for v in p.values)
                parts.append(f"CAST({column} AS VARCHAR) IN ({values})")
            elif p.op == "between":
                parts.append(
                    f"{column} BETWEEN {_literal(p.values[0])} AND {_literal(p.values[1])}"
                )
            elif p.op == "=":
                parts.append(f"CAST({column} AS VARCHAR) = {_literal(str(p.values[0]))}")
            elif p.op == "<>":
                parts.append(
                    f"(CAST({column} AS VARCHAR) <> {_literal(str(p.values[0]))} "
                    f"OR {column} IS NULL)"
                )
            else:
                parts.append(f"{column} {p.op} {_literal(p.values[0])}")
        return " WHERE " + " AND ".join(parts)

    def _run_scalar(self, sql: str) -> Any:
        self.sql_log.append(sql)
        try:
            row = self.con.execute(sql).fetchone()
        except Exception as exc:  # noqa: BLE001 - surfaced with the SQL
            raise EvaluationFailed(f"{type(exc).__name__}: {exc}") from exc
        return None if row is None else row[0]

    def _aggregate(self, name: str, ref: ColumnRef, ctx: FilterContext) -> Any:
        table = self._resolve_table(ref.table)
        if table.column(ref.column) is None:
            raise EvaluationFailed(
                f"'{table.name}' has no column '{ref.column}'"
            )
        needed = {p.table for p in ctx.predicates}
        join_sql, notes = self._join_clause(table, needed)
        self.notes.extend(notes)
        column = f"{_quote(table.name)}.{_quote(ref.column)}"
        template = SQL_AGGREGATIONS[name]
        aggregate = (
            template.format(col=column) if "{col}" in template
            else f"{template}({column})"
        )
        # Cast text-typed numeric columns so a CSV round-trip does not break SUM.
        if name in {"SUM", "AVERAGE", "MIN", "MAX", "MEDIAN"} or name.startswith(
            ("STDEV", "VAR")
        ):
            aggregate = aggregate.replace(column, f"TRY_CAST({column} AS DOUBLE)")
        sql = (
            f"SELECT {aggregate} FROM {_quote(self.sql_names[table.name])} "
            f"AS {_quote(table.name)} {join_sql}{self._where_clause(list(ctx.predicates))}"
        )
        value = self._run_scalar(sql)
        return None if value is None else float(value)

    def _count_rows(self, node: FunctionCall, ctx: FilterContext, *, home: str) -> Any:
        if not node.args:
            if not home:
                raise Unsupported("COUNTROWS with no table", "no home table is known")
            target: Expr = TableRef(name=home)
        else:
            target = node.args[0]

        if isinstance(target, TableRef):
            table = self._resolve_table(target.name)
            join_sql, notes = self._join_clause(
                table, {p.table for p in ctx.predicates}
            )
            self.notes.extend(notes)
            sql = (
                f"SELECT COUNT(*) FROM {_quote(self.sql_names[table.name])} "
                f"AS {_quote(table.name)} {join_sql}"
                f"{self._where_clause(list(ctx.predicates))}"
            )
            return float(self._run_scalar(sql) or 0)

        if isinstance(target, FunctionCall) and target.name.upper() in {
            "DISTINCT", "VALUES"
        } and target.args and isinstance(target.args[0], ColumnRef):
            ref = target.args[0]
            table = self._resolve_table(ref.table or home)
            join_sql, notes = self._join_clause(table, {p.table for p in ctx.predicates})
            self.notes.extend(notes)
            sql = (
                f"SELECT COUNT(DISTINCT {_quote(table.name)}.{_quote(ref.column)}) "
                f"FROM {_quote(self.sql_names[table.name])} AS {_quote(table.name)} "
                f"{join_sql}{self._where_clause(list(ctx.predicates))}"
            )
            return float(self._run_scalar(sql) or 0)

        if isinstance(target, FunctionCall) and target.name.upper() == "FILTER":
            if len(target.args) != 2 or not isinstance(target.args[0], TableRef):
                raise Unsupported(
                    "FILTER form", "only FILTER(<table>, <simple predicate>) is evaluable"
                )
            table = self._resolve_table(target.args[0].name)
            extra = self._predicates_from(target.args[1], default_table=table.name)
            inner = ctx.with_predicates(extra)
            join_sql, notes = self._join_clause(table, {p.table for p in inner.predicates})
            self.notes.extend(notes)
            sql = (
                f"SELECT COUNT(*) FROM {_quote(self.sql_names[table.name])} "
                f"AS {_quote(table.name)} {join_sql}"
                f"{self._where_clause(list(inner.predicates))}"
            )
            return float(self._run_scalar(sql) or 0)

        raise Unsupported("COUNTROWS argument", to_source(target))

    def _selected_value(self, node: FunctionCall, ctx: FilterContext) -> Any:
        ref = node.args[0]
        assert isinstance(ref, ColumnRef)
        table = self._resolve_table(ref.table)
        join_sql, _ = self._join_clause(table, {p.table for p in ctx.predicates})
        sql = (
            f"SELECT CASE WHEN COUNT(DISTINCT {_quote(table.name)}.{_quote(ref.column)}) = 1 "
            f"THEN MIN(CAST({_quote(table.name)}.{_quote(ref.column)} AS VARCHAR)) END "
            f"FROM {_quote(self.sql_names[table.name])} AS {_quote(table.name)} "
            f"{join_sql}{self._where_clause(list(ctx.predicates))}"
        )
        value = self._run_scalar(sql)
        if value is None and len(node.args) > 1:
            return self._eval(node.args[1], ctx)
        return value

    # -- filter modifiers ----------------------------------------------
    def _apply_modifier(
        self, modifier: Expr, ctx: FilterContext, *, home: str
    ) -> FilterContext:
        if isinstance(modifier, FunctionCall):
            name = modifier.name.upper()
            if name in {"ALL", "REMOVEFILTERS"}:
                if not modifier.args:
                    return FilterContext(cleared_tables=frozenset({"*"}))
                tables: list[str] = []
                for arg in modifier.args:
                    if isinstance(arg, TableRef):
                        tables.append(arg.name)
                    elif isinstance(arg, ColumnRef) and arg.table:
                        tables.append(arg.table)
                return ctx.cleared(tables)
            if name == "ALLEXCEPT":
                raise Unsupported("ALLEXCEPT", "not evaluable in this subset")
            if name == "FILTER":
                if len(modifier.args) == 2 and isinstance(modifier.args[0], TableRef):
                    return ctx.with_predicates(
                        self._predicates_from(
                            modifier.args[1], default_table=modifier.args[0].name
                        )
                    )
                raise Unsupported("FILTER form", to_source(modifier))
            if name == "KEEPFILTERS" and modifier.args:
                return self._apply_modifier(modifier.args[0], ctx, home=home)
            if name == "USERELATIONSHIP":
                self.notes.append(
                    "USERELATIONSHIP was ignored by the evaluator: an inactive "
                    "relationship cannot be activated in this subset, so the result "
                    "reflects the ACTIVE relationship."
                )
                return ctx
            if name in TIME_MODIFIERS:
                return self._time_filter(name, modifier, ctx)
        return ctx.with_predicates(self._predicates_from(modifier, default_table=home))

    def _predicates_from(self, node: Expr, *, default_table: str) -> list[Predicate]:
        """Turn a boolean expression into SQL predicates, or refuse."""
        if isinstance(node, BinaryOp):
            if node.op in {"&&", "AND"}:
                return (
                    self._predicates_from(node.left, default_table=default_table)
                    + self._predicates_from(node.right, default_table=default_table)
                )
            if node.op in {"=", "<>", "<", ">", "<=", ">="}:
                ref, value_node = node.left, node.right
                if not isinstance(ref, ColumnRef):
                    ref, value_node = node.right, node.left
                    flip = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}
                    op = flip.get(node.op, node.op)
                else:
                    op = node.op
                if not isinstance(ref, ColumnRef):
                    raise Unsupported(
                        "filter predicate", f"{to_source(node)} compares two expressions"
                    )
                value = self._constant(value_node)
                table = ref.table or default_table
                if not table:
                    raise Unsupported("unqualified filter column", f"[{ref.column}]")
                return [Predicate(table=table, column=ref.column, op=op, values=(value,))]
            if node.op in {"||", "OR"}:
                raise Unsupported(
                    "OR in a filter", "only AND-combined predicates are evaluable"
                )
        if isinstance(node, InOp):
            if not isinstance(node.left, ColumnRef):
                raise Unsupported("IN predicate", to_source(node))
            if node.table is not None:
                raise Unsupported("IN <table>", "only IN {literal, ...} is evaluable")
            table = node.left.table or default_table
            values = tuple(str(self._constant(v)) for v in node.values)
            return [
                Predicate(table=table, column=node.left.column, op="in", values=values)
            ]
        if isinstance(node, BoolLit):
            return []
        raise Unsupported("filter argument", to_source(node))

    def _constant(self, node: Expr) -> Any:
        if isinstance(node, NumberLit):
            return node.value
        if isinstance(node, StringLit):
            return node.value
        if isinstance(node, BoolLit):
            return node.value
        if isinstance(node, BlankLit):
            return None
        if isinstance(node, UnaryOp) and node.op == "-":
            inner = self._constant(node.operand)
            return -inner if isinstance(inner, (int, float)) else inner
        if isinstance(node, FunctionCall) and node.name.upper() == "DATE" and \
                len(node.args) == 3:
            y, m, d = (int(self._constant(a)) for a in node.args)
            return f"{y:04d}-{m:02d}-{d:02d}"
        raise Unsupported("non-constant filter value", to_source(node))

    def _date_column(self) -> tuple[str, str]:
        date_tables = self.model.date_tables
        if not date_tables:
            raise Unsupported("time intelligence", "the model has no date table")
        table = date_tables[0]
        column = next(
            (c for c in table.columns if c.data_type.is_temporal), None
        )
        if column is None:
            raise Unsupported("time intelligence", f"'{table.name}' has no date column")
        return table.name, column.name

    def _context_date_bounds(self, ctx: FilterContext) -> tuple[str, str]:
        """The min/max date currently in context, which time intelligence
        shifts relative to."""
        table_name, column = self._date_column()
        if table_name not in self.sql_names:
            raise Unsupported("missing dataset", f"no data loaded for '{table_name}'")
        predicates = [p for p in ctx.predicates if p.table == table_name]
        sql = (
            f"SELECT MIN(TRY_CAST({_quote(table_name)}.{_quote(column)} AS DATE)), "
            f"MAX(TRY_CAST({_quote(table_name)}.{_quote(column)} AS DATE)) "
            f"FROM {_quote(self.sql_names[table_name])} AS {_quote(table_name)}"
            f"{self._where_clause(predicates)}"
        )
        self.sql_log.append(sql)
        row = self.con.execute(sql).fetchone()
        if not row or row[0] is None:
            raise EvaluationFailed("the date table has no usable dates in context")
        return str(row[0]), str(row[1])

    def _time_filter(
        self, name: str, node: FunctionCall, ctx: FilterContext
    ) -> FilterContext:
        from datetime import date, timedelta

        table_name, column = self._date_column()
        lo_text, hi_text = self._context_date_bounds(ctx)
        lo, hi = date.fromisoformat(lo_text), date.fromisoformat(hi_text)
        base = ctx.cleared([table_name])

        def between(a: date, b: date) -> FilterContext:
            return base.with_predicates([
                Predicate(table=table_name, column=column, op="between",
                          values=(a.isoformat(), b.isoformat()))
            ])

        if name == "DATESYTD":
            return between(date(hi.year, 1, 1), hi)
        if name == "DATESQTD":
            quarter_start_month = 3 * ((hi.month - 1) // 3) + 1
            return between(date(hi.year, quarter_start_month, 1), hi)
        if name == "DATESMTD":
            return between(date(hi.year, hi.month, 1), hi)
        if name == "SAMEPERIODLASTYEAR":
            return between(_shift_years(lo, -1), _shift_years(hi, -1))
        if name == "PREVIOUSYEAR":
            return between(date(hi.year - 1, 1, 1), date(hi.year - 1, 12, 31))
        if name == "PREVIOUSMONTH":
            first = date(hi.year, hi.month, 1)
            last_prev = first - timedelta(days=1)
            return between(date(last_prev.year, last_prev.month, 1), last_prev)
        if name == "PREVIOUSQUARTER":
            quarter_start_month = 3 * ((hi.month - 1) // 3) + 1
            first = date(hi.year, quarter_start_month, 1)
            last_prev = first - timedelta(days=1)
            prev_start_month = 3 * ((last_prev.month - 1) // 3) + 1
            return between(date(last_prev.year, prev_start_month, 1), last_prev)
        if name == "PREVIOUSDAY":
            previous = lo - timedelta(days=1)
            return between(previous, previous)
        if name == "NEXTMONTH":
            first_next = (date(hi.year, hi.month, 28) + timedelta(days=5)).replace(day=1)
            last_next = (first_next + timedelta(days=36)).replace(day=1) - timedelta(days=1)
            return between(first_next, last_next)
        if name == "NEXTYEAR":
            return between(date(hi.year + 1, 1, 1), date(hi.year + 1, 12, 31))
        if name in {"DATEADD", "PARALLELPERIOD"}:
            if len(node.args) != 3:
                raise Unsupported(name, "expects three arguments")
            offset = int(self._constant(node.args[1]))
            unit = _interval_unit(node.args[2])
            if unit == "YEAR":
                return between(_shift_years(lo, offset), _shift_years(hi, offset))
            if unit == "MONTH":
                return between(_shift_months(lo, offset), _shift_months(hi, offset))
            if unit == "DAY":
                return between(lo + timedelta(days=offset), hi + timedelta(days=offset))
            if unit == "QUARTER":
                return between(_shift_months(lo, offset * 3), _shift_months(hi, offset * 3))
            raise Unsupported(name, f"interval '{unit}' is not supported")
        raise Unsupported("time intelligence", name)

    def _total_to_date(
        self, name: str, node: FunctionCall, ctx: FilterContext, *, home: str,
        variables: dict[str, Any],
    ) -> Any:
        wrapper = {"TOTALYTD": "DATESYTD", "TOTALQTD": "DATESQTD", "TOTALMTD": "DATESMTD"}[name]
        new_ctx = self._time_filter(wrapper, node, ctx)
        for extra in node.args[2:]:
            new_ctx = self._apply_modifier(extra, new_ctx, home=home)
        return self._eval(node.args[0], new_ctx, home=home, variables=variables)


def _shift_years(value, years: int):  # noqa: ANN001, ANN201
    from datetime import date
    try:
        return value.replace(year=value.year + years)
    except ValueError:  # 29 Feb
        return date(value.year + years, value.month, 28)


def _shift_months(value, months: int):  # noqa: ANN001, ANN201
    from calendar import monthrange
    from datetime import date

    total = value.year * 12 + (value.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def _interval_unit(node: Expr) -> str:
    if isinstance(node, (TableRef, VarRef)):
        return node.name.upper()
    if isinstance(node, StringLit):
        return node.value.upper()
    if isinstance(node, ColumnRef) and not node.table:
        return node.column.upper()
    raise Unsupported("interval unit", to_source(node))
