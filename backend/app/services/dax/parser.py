"""A recursive-descent DAX parser.

Produces an AST that the reference validator walks to resolve names, and that
the DuckDB transpiler walks to build SQL. Having a real tree (rather than
regex-scraping the expression) is what makes it possible to say *where* a
problem is and to know whether a column sits inside an aggregation.

Precedence, lowest to highest:

    ``VAR``/``RETURN`` block
    ``||`` / ``OR``
    ``&&`` / ``AND``
    comparison (``=`` ``<>`` ``<`` ``>`` ``<=`` ``>=``, and ``IN``)
    ``&``  (text concatenation)
    ``+`` ``-``
    ``*`` ``/``
    unary ``-`` ``+`` ``NOT``
    ``^``
    primary

Not supported, and reported as such rather than mis-parsed: the DAX query
language (``DEFINE`` / ``EVALUATE``), table constructors using ``{...}``
braces, and ``ORDER BY``. Those belong to queries, not measures.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from .lexer import DaxSyntaxError, Token, TokenKind, tokenise


# --------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------
@dataclass
class Node:
    position: int = 0


@dataclass
class NumberLit(Node):
    value: float = 0.0


@dataclass
class StringLit(Node):
    value: str = ""


@dataclass
class BoolLit(Node):
    value: bool = True


@dataclass
class BlankLit(Node):
    pass


@dataclass
class ColumnRef(Node):
    """``Table[Column]`` or a bare ``[Column]`` inside a row context."""

    table: str = ""
    column: str = ""

    @property
    def qualified(self) -> bool:
        return bool(self.table)

    def __str__(self) -> str:
        return f"{self.table}[{self.column}]" if self.table else f"[{self.column}]"


@dataclass
class MeasureRef(Node):
    """A bare ``[Name]`` that resolves to a measure.

    The parser cannot distinguish ``[Total Cost]`` (a measure) from
    ``[cost_amount]`` (a column in the current row context) -- that needs the
    model. It therefore emits ``ColumnRef`` with no table and the validator
    decides, promoting it to a measure reference when the name matches one.
    """

    name: str = ""

    def __str__(self) -> str:
        return f"[{self.name}]"


@dataclass
class TableRef(Node):
    name: str = ""

    def __str__(self) -> str:
        return self.name


@dataclass
class VarRef(Node):
    name: str = ""


@dataclass
class FunctionCall(Node):
    name: str = ""
    args: list["Expr"] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.name}(...)"


@dataclass
class UnaryOp(Node):
    op: str = ""
    operand: "Expr" = None  # type: ignore[assignment]


@dataclass
class BinaryOp(Node):
    op: str = ""
    left: "Expr" = None  # type: ignore[assignment]
    right: "Expr" = None  # type: ignore[assignment]


@dataclass
class InOp(Node):
    """``expr IN {a, b, c}`` or ``expr IN table``."""

    left: "Expr" = None  # type: ignore[assignment]
    values: list["Expr"] = field(default_factory=list)
    table: "Expr | None" = None


@dataclass
class VarBlock(Node):
    variables: list[tuple[str, "Expr"]] = field(default_factory=list)
    body: "Expr" = None  # type: ignore[assignment]


Expr = Union[
    NumberLit, StringLit, BoolLit, BlankLit, ColumnRef, MeasureRef, TableRef,
    VarRef, FunctionCall, UnaryOp, BinaryOp, InOp, VarBlock,
]


class UnsupportedDax(DaxSyntaxError):
    """Valid DAX that this parser deliberately does not handle."""


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------
class Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = tokenise(source)
        self.i = 0
        self.var_names: set[str] = set()

    # -- token helpers --------------------------------------------------
    @property
    def current(self) -> Token:
        return self.tokens[self.i]

    def peek(self, offset: int = 1) -> Token:
        j = min(self.i + offset, len(self.tokens) - 1)
        return self.tokens[j]

    def advance(self) -> Token:
        token = self.tokens[self.i]
        if token.kind is not TokenKind.EOF:
            self.i += 1
        return token

    def at_operator(self, *ops: str) -> bool:
        return (
            self.current.kind is TokenKind.OPERATOR and self.current.value in ops
        )

    def at_keyword(self, *words: str) -> bool:
        return (
            self.current.kind is TokenKind.KEYWORD
            and self.current.value.lower() in {w.lower() for w in words}
        )

    def expect(self, kind: TokenKind, what: str) -> Token:
        if self.current.kind is not kind:
            raise DaxSyntaxError(
                f"expected {what} but found {self._describe(self.current)}",
                self.current.position, self.current.line,
            )
        return self.advance()

    @staticmethod
    def _describe(token: Token) -> str:
        if token.kind is TokenKind.EOF:
            return "the end of the expression"
        return f"{token.kind.value} {token.value!r}"

    # -- entry point ----------------------------------------------------
    def parse(self) -> Expr:
        if self.at_keyword("define", "evaluate"):
            raise UnsupportedDax(
                "DEFINE/EVALUATE is the DAX query language, not a measure expression. "
                "A measure body is a single scalar expression",
                self.current.position, self.current.line,
            )
        node = self.parse_expression()
        if self.current.kind is not TokenKind.EOF:
            raise DaxSyntaxError(
                f"unexpected {self._describe(self.current)} after a complete expression",
                self.current.position, self.current.line,
            )
        return node

    def parse_expression(self) -> Expr:
        if self.at_keyword("var"):
            return self.parse_var_block()
        return self.parse_or()

    def parse_var_block(self) -> Expr:
        start = self.current.position
        variables: list[tuple[str, Expr]] = []
        while self.at_keyword("var"):
            self.advance()
            name_token = self.current
            if name_token.kind not in {TokenKind.IDENT, TokenKind.BRACKETED}:
                raise DaxSyntaxError(
                    f"expected a variable name after VAR but found "
                    f"{self._describe(name_token)}",
                    name_token.position, name_token.line,
                )
            self.advance()
            if not self.at_operator("="):
                raise DaxSyntaxError(
                    f"expected '=' after the variable name {name_token.value!r}",
                    self.current.position, self.current.line,
                )
            self.advance()
            self.var_names.add(name_token.value.lower())
            variables.append((name_token.value, self.parse_or()))
        if not self.at_keyword("return"):
            raise DaxSyntaxError(
                "a VAR block must end with RETURN", self.current.position, self.current.line
            )
        self.advance()
        body = self.parse_expression()
        return VarBlock(position=start, variables=variables, body=body)

    def parse_or(self) -> Expr:
        left = self.parse_and()
        while self.at_operator("||") or self.at_keyword("or"):
            token = self.advance()
            op = "||" if token.value == "||" else "OR"
            left = BinaryOp(position=token.position, op=op, left=left, right=self.parse_and())
        return left

    def parse_and(self) -> Expr:
        left = self.parse_comparison()
        while self.at_operator("&&") or self.at_keyword("and"):
            token = self.advance()
            op = "&&" if token.value == "&&" else "AND"
            left = BinaryOp(
                position=token.position, op=op, left=left, right=self.parse_comparison()
            )
        return left

    def parse_comparison(self) -> Expr:
        left = self.parse_concat()
        while True:
            if self.at_operator("=", "<>", "<", ">", "<=", ">="):
                token = self.advance()
                left = BinaryOp(
                    position=token.position, op=token.value, left=left,
                    right=self.parse_concat(),
                )
                continue
            if self.at_keyword("in"):
                token = self.advance()
                left = self.parse_in_tail(left, token.position)
                continue
            if self.at_keyword("not") and self.peek().kind is TokenKind.KEYWORD \
                    and self.peek().value.lower() == "in":
                token = self.advance()
                self.advance()
                inner = self.parse_in_tail(left, token.position)
                left = UnaryOp(position=token.position, op="NOT", operand=inner)
                continue
            break
        return left

    def parse_in_tail(self, left: Expr, position: int) -> Expr:
        """``IN {a, b}``, or ``IN <table expression>``."""
        if self.current.kind is TokenKind.LBRACE:
            self.advance()
            values: list[Expr] = []
            while self.current.kind is not TokenKind.RBRACE:
                if self.current.kind is TokenKind.EOF:
                    raise DaxSyntaxError(
                        "unterminated '{' in an IN list",
                        self.current.position, self.current.line,
                    )
                values.append(self.parse_or())
                if self.current.kind is TokenKind.COMMA:
                    self.advance()
                elif self.current.kind is not TokenKind.RBRACE:
                    raise DaxSyntaxError(
                        "expected ',' or '}' in an IN list",
                        self.current.position, self.current.line,
                    )
            self.advance()
            return InOp(position=position, left=left, values=values)
        return InOp(position=position, left=left, values=[], table=self.parse_concat())

    def parse_concat(self) -> Expr:
        left = self.parse_additive()
        while self.at_operator("&"):
            token = self.advance()
            left = BinaryOp(
                position=token.position, op="&", left=left, right=self.parse_additive()
            )
        return left

    def parse_additive(self) -> Expr:
        left = self.parse_multiplicative()
        while self.at_operator("+", "-"):
            token = self.advance()
            left = BinaryOp(
                position=token.position, op=token.value, left=left,
                right=self.parse_multiplicative(),
            )
        return left

    def parse_multiplicative(self) -> Expr:
        left = self.parse_unary()
        while self.at_operator("*", "/"):
            token = self.advance()
            left = BinaryOp(
                position=token.position, op=token.value, left=left, right=self.parse_unary()
            )
        return left

    def parse_unary(self) -> Expr:
        if self.at_operator("-", "+"):
            token = self.advance()
            return UnaryOp(position=token.position, op=token.value, operand=self.parse_unary())
        if self.at_keyword("not"):
            token = self.advance()
            return UnaryOp(position=token.position, op="NOT", operand=self.parse_unary())
        return self.parse_power()

    def parse_power(self) -> Expr:
        base = self.parse_primary()
        if self.at_operator("^"):
            token = self.advance()
            return BinaryOp(
                position=token.position, op="^", left=base, right=self.parse_unary()
            )
        return base

    def parse_primary(self) -> Expr:
        token = self.current

        if token.kind is TokenKind.NUMBER:
            self.advance()
            return NumberLit(position=token.position, value=float(token.value))

        if token.kind is TokenKind.STRING:
            self.advance()
            return StringLit(position=token.position, value=token.value)

        if token.kind is TokenKind.LPAREN:
            self.advance()
            inner = self.parse_expression()
            self.expect(TokenKind.RPAREN, "')'")
            return inner

        if token.kind is TokenKind.KEYWORD:
            word = token.value.lower()
            if word in {"true", "false"}:
                self.advance()
                # TRUE()/FALSE() are also valid
                if self.current.kind is TokenKind.LPAREN:
                    self.advance()
                    self.expect(TokenKind.RPAREN, "')'")
                return BoolLit(position=token.position, value=word == "true")
            if word == "blank":
                self.advance()
                if self.current.kind is TokenKind.LPAREN:
                    self.advance()
                    self.expect(TokenKind.RPAREN, "')' -- BLANK takes no arguments")
                return BlankLit(position=token.position)
            if word in {"return", "in"}:
                raise DaxSyntaxError(
                    f"{token.value.upper()} appears without a matching VAR block",
                    token.position, token.line,
                )
            raise DaxSyntaxError(
                f"{token.value.upper()} cannot start an expression",
                token.position, token.line,
            )

        # Table[Column]
        if token.kind in {TokenKind.IDENT, TokenKind.QUOTED_TABLE}:
            if self.peek().kind is TokenKind.BRACKETED:
                self.advance()
                bracket = self.advance()
                return ColumnRef(
                    position=token.position, table=token.value, column=bracket.value
                )
            if self.peek().kind is TokenKind.LPAREN:
                return self.parse_function_call()
            self.advance()
            if token.kind is TokenKind.IDENT and token.value.lower() in self.var_names:
                return VarRef(position=token.position, name=token.value)
            # A bare name with no brackets and no call: a table reference.
            return TableRef(position=token.position, name=token.value)

        # bare [Name] -- a measure or a row-context column; the validator decides
        if token.kind is TokenKind.BRACKETED:
            self.advance()
            return ColumnRef(position=token.position, table="", column=token.value)

        if token.kind is TokenKind.LBRACE:
            raise UnsupportedDax(
                "a '{...}' table constructor is only supported directly after IN",
                token.position, token.line,
            )

        if token.kind is TokenKind.EOF:
            raise DaxSyntaxError(
                "the expression ended unexpectedly", token.position, token.line
            )

        raise DaxSyntaxError(
            f"unexpected {self._describe(token)}", token.position, token.line
        )

    def parse_function_call(self) -> Expr:
        name_token = self.expect(TokenKind.IDENT, "a function name")
        self.expect(TokenKind.LPAREN, "'(' after a function name")
        args: list[Expr] = []
        if self.current.kind is not TokenKind.RPAREN:
            while True:
                args.append(self.parse_expression())
                if self.current.kind is TokenKind.COMMA:
                    self.advance()
                    continue
                break
        if self.current.kind is not TokenKind.RPAREN:
            raise DaxSyntaxError(
                f"expected ',' or ')' in the arguments to {name_token.value} but found "
                f"{self._describe(self.current)}",
                self.current.position, self.current.line,
            )
        self.advance()
        return FunctionCall(position=name_token.position, name=name_token.value, args=args)


def parse(source: str) -> Expr:
    """Parse a measure expression. Raises ``DaxSyntaxError`` on failure."""
    if not source or not source.strip():
        raise DaxSyntaxError("the expression is empty", 0)
    return Parser(source).parse()


# --------------------------------------------------------------------------
# Tree walking
# --------------------------------------------------------------------------
def walk(node: Expr):  # noqa: ANN201
    """Yield every node, depth first."""
    yield node
    if isinstance(node, (UnaryOp,)):
        yield from walk(node.operand)
    elif isinstance(node, BinaryOp):
        yield from walk(node.left)
        yield from walk(node.right)
    elif isinstance(node, FunctionCall):
        for arg in node.args:
            yield from walk(arg)
    elif isinstance(node, InOp):
        yield from walk(node.left)
        for v in node.values:
            yield from walk(v)
        if node.table is not None:
            yield from walk(node.table)
    elif isinstance(node, VarBlock):
        for _, value in node.variables:
            yield from walk(value)
        yield from walk(node.body)


def function_calls(node: Expr) -> list[FunctionCall]:
    return [n for n in walk(node) if isinstance(n, FunctionCall)]


def column_refs(node: Expr) -> list[ColumnRef]:
    return [n for n in walk(node) if isinstance(n, ColumnRef)]


def table_refs(node: Expr) -> list[TableRef]:
    return [n for n in walk(node) if isinstance(n, TableRef)]


def to_source(node: Expr) -> str:
    """Render an AST back to DAX. Used to normalise formatting and to show
    the user exactly what was validated."""
    if isinstance(node, NumberLit):
        return f"{node.value:g}"
    if isinstance(node, StringLit):
        escaped = node.value.replace('"', '""')
        return f'"{escaped}"'
    if isinstance(node, BoolLit):
        return "TRUE()" if node.value else "FALSE()"
    if isinstance(node, BlankLit):
        return "BLANK()"
    if isinstance(node, ColumnRef):
        if not node.table:
            return f"[{node.column}]"
        table = node.table
        if not table.replace("_", "").isalnum():
            table = f"'{table}'"
        return f"{table}[{node.column}]"
    if isinstance(node, MeasureRef):
        return f"[{node.name}]"
    if isinstance(node, TableRef):
        return node.name
    if isinstance(node, VarRef):
        return node.name
    if isinstance(node, FunctionCall):
        return f"{node.name.upper()}({', '.join(to_source(a) for a in node.args)})"
    if isinstance(node, UnaryOp):
        space = " " if node.op == "NOT" else ""
        return f"{node.op}{space}{to_source(node.operand)}"
    if isinstance(node, BinaryOp):
        return f"{to_source(node.left)} {node.op} {to_source(node.right)}"
    if isinstance(node, InOp):
        if node.table is not None:
            return f"{to_source(node.left)} IN {to_source(node.table)}"
        return f"{to_source(node.left)} IN {{{', '.join(to_source(v) for v in node.values)}}}"
    if isinstance(node, VarBlock):
        parts = [f"VAR {name} = {to_source(value)}" for name, value in node.variables]
        parts.append(f"RETURN {to_source(node.body)}")
        return "\n".join(parts)
    return "<?>"
