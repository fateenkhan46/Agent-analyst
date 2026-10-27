"""A DAX lexer.

Written by hand because there is no DAX parser available for Python and
because validating LLM-generated DAX by "does it look plausible" is exactly
the failure mode the brief warns about. A real tokeniser plus the parser in
``parser.py`` lets us reject malformed expressions with a position and a
reason.

DAX specifics handled here:

* identifiers in three forms -- bare (``SUM``), bracketed (``[Total Cost]``)
  and quoted-table (``'Fact Cost'``);
* ``Table[Column]`` as two adjacent tokens, resolved by the parser;
* ``--`` is **not** a comment in DAX (it is double negation); only ``//`` and
  ``/* */`` are comments;
* ``&&``, ``||``, ``<=``, ``>=``, ``<>``, ``&`` and ``^`` as operators;
* string literals in double quotes with ``""`` escaping.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TokenKind(str, Enum):
    NUMBER = "number"
    STRING = "string"
    IDENT = "ident"              # bare: SUM, CALCULATE, VAR names
    BRACKETED = "bracketed"      # [Total Cost]  -> column or measure
    QUOTED_TABLE = "quoted_table"  # 'Fact Cost'
    OPERATOR = "operator"
    LPAREN = "lparen"
    RPAREN = "rparen"
    LBRACE = "lbrace"      # table constructor: x IN {1, 2}
    RBRACE = "rbrace"
    COMMA = "comma"
    KEYWORD = "keyword"
    EOF = "eof"


KEYWORDS = {
    "var", "return", "in", "not", "and", "or", "true", "false", "blank",
    "order", "by", "asc", "desc", "define", "measure", "evaluate", "start", "at",
}

OPERATORS = (
    "<=", ">=", "<>", "&&", "||", "=", "<", ">", "+", "-", "*", "/", "^", "&",
)


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    value: str
    position: int
    line: int = 1

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.kind.value}({self.value!r})@{self.position}"


class DaxSyntaxError(Exception):
    """A lexical or syntactic error, with the offending position."""

    def __init__(self, message: str, position: int, line: int = 1) -> None:
        super().__init__(f"{message} (line {line}, position {position})")
        self.message = message
        self.position = position
        self.line = line


def tokenise(source: str) -> list[Token]:
    tokens: list[Token] = []
    i, line, n = 0, 1, len(source)

    while i < n:
        ch = source[i]

        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch in " \t\r":
            i += 1
            continue

        # comments: // to end of line, /* ... */ block.
        # NOTE: "--" is double negation in DAX, not a comment.
        if source.startswith("//", i):
            j = source.find("\n", i)
            i = n if j == -1 else j
            continue
        if source.startswith("/*", i):
            j = source.find("*/", i + 2)
            if j == -1:
                raise DaxSyntaxError("unterminated block comment", i, line)
            line += source.count("\n", i, j)
            i = j + 2
            continue

        # string literal
        if ch == '"':
            j, buf = i + 1, []
            while j < n:
                if source[j] == '"':
                    if j + 1 < n and source[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(source[j])
                j += 1
            if j >= n:
                raise DaxSyntaxError("unterminated string literal", i, line)
            tokens.append(Token(TokenKind.STRING, "".join(buf), i, line))
            i = j + 1
            continue

        # bracketed identifier: [Total Cost]
        if ch == "[":
            j = source.find("]", i + 1)
            if j == -1:
                raise DaxSyntaxError("unterminated '[' identifier", i, line)
            name = source[i + 1:j]
            if not name.strip():
                raise DaxSyntaxError("empty '[]' identifier", i, line)
            tokens.append(Token(TokenKind.BRACKETED, name, i, line))
            i = j + 1
            continue

        # quoted table name: 'Fact Cost'
        if ch == "'":
            j, buf = i + 1, []
            while j < n:
                if source[j] == "'":
                    if j + 1 < n and source[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(source[j])
                j += 1
            if j >= n:
                raise DaxSyntaxError("unterminated quoted table name", i, line)
            tokens.append(Token(TokenKind.QUOTED_TABLE, "".join(buf), i, line))
            i = j + 1
            continue

        # number (including a leading decimal point)
        if ch.isdigit() or (ch == "." and i + 1 < n and source[i + 1].isdigit()):
            j = i
            seen_dot = False
            seen_exp = False
            while j < n:
                c = source[j]
                if c.isdigit():
                    j += 1
                elif c == "." and not seen_dot and not seen_exp:
                    seen_dot = True
                    j += 1
                elif c in "eE" and not seen_exp and j + 1 < n and (
                    source[j + 1].isdigit() or source[j + 1] in "+-"
                ):
                    seen_exp = True
                    j += 2
                else:
                    break
            tokens.append(Token(TokenKind.NUMBER, source[i:j], i, line))
            i = j
            continue

        # bare identifier / keyword
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (source[j].isalnum() or source[j] in "_."):
                j += 1
            word = source[i:j]
            kind = TokenKind.KEYWORD if word.lower() in KEYWORDS else TokenKind.IDENT
            tokens.append(Token(kind, word, i, line))
            i = j
            continue

        if ch == "(":
            tokens.append(Token(TokenKind.LPAREN, ch, i, line))
            i += 1
            continue
        if ch == ")":
            tokens.append(Token(TokenKind.RPAREN, ch, i, line))
            i += 1
            continue
        if ch == "{":
            tokens.append(Token(TokenKind.LBRACE, ch, i, line))
            i += 1
            continue
        if ch == "}":
            tokens.append(Token(TokenKind.RBRACE, ch, i, line))
            i += 1
            continue
        if ch in ",;":
            tokens.append(Token(TokenKind.COMMA, ",", i, line))
            i += 1
            continue

        matched = next((op for op in OPERATORS if source.startswith(op, i)), None)
        if matched:
            tokens.append(Token(TokenKind.OPERATOR, matched, i, line))
            i += len(matched)
            continue

        raise DaxSyntaxError(f"unexpected character {ch!r}", i, line)

    tokens.append(Token(TokenKind.EOF, "", n, line))
    return tokens
