"""Deterministic type and format inference.

This module answers two questions for a column of raw strings:

1. **What is this, really?** (``CanonicalType``)
2. **How many different surface forms is it written in?** (``FormatVariant``)

Question 2 is the one that matters for the brief: reporting that a column
"has inconsistent date formats" is only useful if we name the competing
patterns and count them, because that is what drives the Power Query M we
later generate. So every classifier returns the *pattern it matched*, not just
a boolean.

Nothing here calls an LLM. Results are reproducible for a given input.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

from ...schemas.common import CanonicalType

# --------------------------------------------------------------------------
# Null-ish tokens seen in real business exports
# --------------------------------------------------------------------------
NULL_TOKENS = {
    "", "-", "--", "n/a", "na", "nan", "none", "null", "nil", "#n/a", "#na",
    "#value!", "#ref!", "#div/0!", "(blank)", "blank", "unknown", "tbd", ".",
    "not available", "no data", "?",
    # pandas sentinels. A parsed-but-invalid date becomes NaT, and a missing
    # value in a nullable column becomes <NA>; without these a null date is
    # invisible to the quality measurement and to the DAX evaluator.
    "nat", "<na>", "nattype",
}

#: Deliberately narrow. Words like "Active", "Complete" or "Closed" are
#: status values in business data far more often than they are booleans;
#: classifying them as boolean would hide them from category/key analysis.
TRUE_TOKENS = {"true", "yes", "y", "t"}
FALSE_TOKENS = {"false", "no", "n", "f"}

CURRENCY_SYMBOLS = {
    "$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY", "₹": "INR",
    "₩": "KRW", "₽": "RUB", "R$": "BRL", "CHF": "CHF", "A$": "AUD", "C$": "CAD",
}
CURRENCY_CODES = {
    "USD", "EUR", "GBP", "JPY", "INR", "AUD", "CAD", "CHF", "CNY", "SEK",
    "NZD", "SGD", "AED", "ZAR", "BRL", "MXN",
}

MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


# --------------------------------------------------------------------------
# Date formats, most specific first.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class DatePattern:
    name: str
    regex: re.Pattern[str]
    strptime: str | None
    description: str
    #: True when the day/month order genuinely cannot be determined from the
    #: value alone (e.g. 03/04/2024). Such columns get an explicit ambiguity
    #: issue instead of a silent guess.
    ambiguous_dmy: bool = False


DATE_PATTERNS: tuple[DatePattern, ...] = (
    DatePattern("iso_datetime", re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?"),
                None, "ISO 8601 date-time (2024-03-15 09:30:00)"),
    DatePattern("iso_date", re.compile(r"^\d{4}-\d{2}-\d{2}$"),
                "%Y-%m-%d", "ISO date (2024-03-15)"),
    DatePattern("iso_compact", re.compile(r"^\d{8}$"),
                "%Y%m%d", "Compact ISO (20240315)"),
    DatePattern("ymd_slash", re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$"),
                "%Y/%m/%d", "Year/month/day (2024/03/15)"),
    DatePattern("dmy_dot", re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$"),
                "%d.%m.%Y", "European dotted (15.03.2024)"),
    DatePattern("dmy_slash_unambiguous", re.compile(r"^(1[3-9]|2\d|3[01])/\d{1,2}/\d{4}$"),
                "%d/%m/%Y", "Day/month/year (15/03/2024)"),
    DatePattern("mdy_slash_unambiguous", re.compile(r"^\d{1,2}/(1[3-9]|2\d|3[01])/\d{4}$"),
                "%m/%d/%Y", "Month/day/year (03/15/2024)"),
    DatePattern("slash_ambiguous", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$"),
                None, "Ambiguous day/month order (03/04/2024)", ambiguous_dmy=True),
    DatePattern("dmy_dash_unambiguous", re.compile(r"^(1[3-9]|2\d|3[01])-\d{1,2}-\d{4}$"),
                "%d-%m-%Y", "Day-month-year (15-03-2024)"),
    DatePattern("dash_ambiguous", re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$"),
                None, "Ambiguous dashed day/month order (03-04-2024)", ambiguous_dmy=True),
    DatePattern("dmon_y", re.compile(r"^\d{1,2}[- ][A-Za-z]{3,9}[- ]\d{2,4}$"),
                None, "Day-Month name-Year (15-Mar-2024)"),
    DatePattern("mon_d_y", re.compile(r"^[A-Za-z]{3,9} \d{1,2},? \d{4}$"),
                None, "Month name day, year (March 15, 2024)"),
    DatePattern("mon_y", re.compile(r"^[A-Za-z]{3,9}[- ]\d{2,4}$"),
                None, "Month name and year only (Mar-2024)"),
    DatePattern("y_mon", re.compile(r"^\d{4}[- ][A-Za-z]{3,9}$"),
                None, "Year and month name (2024-Mar)"),
    DatePattern("ym_dash", re.compile(r"^\d{4}-\d{1,2}$"),
                "%Y-%m", "Year-month (2024-03)"),
    DatePattern("quarter", re.compile(r"^(Q[1-4][- /]?\d{2,4}|\d{4}[- ]?Q[1-4])$", re.I),
                None, "Quarter (Q1-2024)"),
    DatePattern("excel_serial", re.compile(r"^(1[0-9]{4}|2[0-9]{4}|3[0-9]{4}|4[0-4][0-9]{3})$"),
                None, "Possible Excel date serial (45000)"),
)

#: Numeric surface forms.
NUMBER_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("plain", re.compile(r"^-?\d+$"), "Plain integer (1234)"),
    ("decimal_point", re.compile(r"^-?\d+\.\d+$"), "Decimal point (1234.56)"),
    ("thousands_comma", re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$"),
     "Comma thousands separator (1,234.56)"),
    ("thousands_dot", re.compile(r"^-?\d{1,3}(\.\d{3})+(,\d+)?$"),
     "European dot thousands / comma decimal (1.234,56)"),
    ("thousands_space", re.compile(r"^-?\d{1,3}([  ]\d{3})+([.,]\d+)?$"),
     "Space thousands separator (1 234,56)"),
    ("decimal_comma", re.compile(r"^-?\d+,\d{1,2}$"), "Comma decimal separator (1234,56)"),
    ("accounting_negative", re.compile(r"^\(\s*[\d,.\s]+\s*\)$"),
     "Accounting negative in parentheses ((1,234.56))"),
    ("trailing_minus", re.compile(r"^[\d,.\s]+-$"), "Trailing minus sign (1234-)"),
    ("scientific", re.compile(r"^-?\d+(\.\d+)?[eE][+-]?\d+$"), "Scientific notation (1.2e5)"),
)

PERCENT_RE = re.compile(r"^-?[\d,.\s]+\s*%$")
CURRENCY_PREFIX_RE = re.compile(r"^\(?\s*([A-Z]{3}|[$£€¥₹₩₽]|R\$|A\$|C\$)\s*-?[\d,.\s]")
CURRENCY_SUFFIX_RE = re.compile(r"[\d,.\s]\s*([A-Z]{3}|[$£€¥₹₩₽])\)?$")
DURATION_RE = re.compile(r"^\d+(\.\d+)?\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes|d|day|days)$", re.I)
HHMM_RE = re.compile(r"^\d{1,3}:[0-5]\d(:[0-5]\d)?$")

IDENTIFIER_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("prefixed_code", re.compile(r"^[A-Za-z]{1,6}[-_ ]?\d{1,10}$"),
     "Letter prefix + number (PRJ-1001, PRJ 1001, prj1001, E1)"),
    ("uuid", re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"), "UUID"),
    ("numeric_code", re.compile(r"^\d{4,12}$"), "Numeric code"),
    ("alnum_code", re.compile(r"^[A-Z0-9]{4,20}$"), "Upper-case alphanumeric code"),
    ("dotted_code", re.compile(r"^[A-Za-z0-9]+(\.[A-Za-z0-9]+){1,4}$"), "Dotted code (AB.12.3)"),
)

#: Pattern names that indicate a code-like value. Used by key discovery to
#: decide whether a STRING column is plausibly a join key.
CODE_LIKE_PATTERNS = frozenset({
    "prefixed_code", "uuid", "numeric_code", "alnum_code", "dotted_code",
    "id_by_name", "id_plain", "id_thousands_comma", "native_int_id",
})

#: Column names that signal an identifier regardless of value shape.
ID_NAME_RE = re.compile(
    r"(^|[_\s\-])(id|ids|code|codes|key|keys|no|num|number|ref|reference|sku|"
    r"account|acct|wbs|ucode)([_\s\-]|$)", re.I
)


def is_nullish(raw: object) -> bool:
    if raw is None:
        return True
    s = str(raw).strip().lower()
    return s in NULL_TOKENS


# --------------------------------------------------------------------------
# Classification of a single value
# --------------------------------------------------------------------------
@dataclass
class ValueClass:
    canonical: CanonicalType
    pattern: str
    description: str
    currency: str = ""
    ambiguous: bool = False


_MONTH_PREFIXES = {k[:3] for k in MONTH_NAMES}


def _contains_month_name(s: str) -> bool:
    return any(
        t[:3] in _MONTH_PREFIXES
        for t in re.split(r"[^A-Za-z]+", s.lower())
        if len(t) >= 3
    )


def _classify_date(s: str) -> ValueClass | None:
    for p in DATE_PATTERNS:
        if not p.regex.match(s):
            continue
        if p.name in {"mon_y", "y_mon"}:
            # These regexes also match things like "PRJ-1001", so require a
            # real month name before calling it a date.
            if not _contains_month_name(s):
                continue
            return ValueClass(CanonicalType.DATE, p.name, p.description)
        if p.name in {"quarter", "ym_dash"}:
            return ValueClass(CanonicalType.DATE, p.name, p.description)
        if p.name == "iso_datetime":
            return ValueClass(CanonicalType.DATETIME, p.name, p.description)
        if p.name == "excel_serial":
            # Only a *possible* serial; the caller decides using the column
            # name and the rest of the column. Never silently converted.
            return ValueClass(CanonicalType.INTEGER, "excel_serial_candidate",
                              p.description, ambiguous=True)
        if p.name in {"dmon_y", "mon_d_y"} and not _contains_month_name(s):
            continue
        return ValueClass(CanonicalType.DATE, p.name, p.description,
                          ambiguous=p.ambiguous_dmy)
    return None


def _detect_currency(s: str) -> str:
    m = CURRENCY_PREFIX_RE.match(s) or CURRENCY_SUFFIX_RE.search(s)
    if not m:
        return ""
    token = m.group(1).strip()
    if token in CURRENCY_SYMBOLS:
        return CURRENCY_SYMBOLS[token]
    if token.upper() in CURRENCY_CODES:
        return token.upper()
    return ""


def _match_number_shape(body: str) -> tuple[str, str] | None:
    for name, rx, desc in NUMBER_PATTERNS:
        if rx.match(body):
            return name, desc
    return None


def classify_value(raw: object, column_name: str = "") -> ValueClass | None:
    """Classify one non-null value. Returns ``None`` when it is null-ish."""
    if is_nullish(raw):
        return None

    if isinstance(raw, bool):
        return ValueClass(CanonicalType.BOOLEAN, "native_bool", "Native boolean")
    if isinstance(raw, (datetime,)):
        return ValueClass(CanonicalType.DATETIME, "native_datetime", "Native datetime")
    if isinstance(raw, date):
        return ValueClass(CanonicalType.DATE, "native_date", "Native date")
    if isinstance(raw, int) and not isinstance(raw, bool):
        if ID_NAME_RE.search(column_name):
            return ValueClass(CanonicalType.IDENTIFIER, "native_int_id",
                              "Integer used as an identifier")
        return ValueClass(CanonicalType.INTEGER, "native_int", "Native integer")
    if isinstance(raw, float):
        return ValueClass(CanonicalType.DECIMAL, "native_float", "Native float")

    s = str(raw).strip()
    low = s.lower()

    # percentage -----------------------------------------------------
    if PERCENT_RE.match(s):
        return ValueClass(CanonicalType.PERCENTAGE, "percent_suffix",
                          "Percentage with % sign (12.5%)")

    # currency -------------------------------------------------------
    cur = _detect_currency(s)
    if cur:
        body = re.sub(r"[A-Z]{3}|[$£€¥₹₩₽]|R\$|A\$|C\$", "", s).strip()
        shape = _match_number_shape(body) or _match_number_shape(body.replace(" ", ""))
        if shape:
            return ValueClass(CanonicalType.CURRENCY, f"currency_{shape[0]}",
                              f"{cur} amount, {shape[1]}", currency=cur)
        return ValueClass(CanonicalType.CURRENCY, "currency_unparsed",
                          f"{cur} amount with unrecognised number shape", currency=cur)

    # duration -------------------------------------------------------
    if DURATION_RE.match(s):
        return ValueClass(CanonicalType.DURATION, "duration_unit",
                          "Number with a time unit suffix (7.5h)")
    if HHMM_RE.match(s):
        return ValueClass(CanonicalType.DURATION, "duration_hhmm",
                          "Hours:minutes duration (07:30)")

    # date -----------------------------------------------------------
    d = _classify_date(s)
    if d and d.pattern != "excel_serial_candidate":
        return d

    # plain number ---------------------------------------------------
    shape = _match_number_shape(s)
    if shape:
        name, desc = shape
        if ID_NAME_RE.search(column_name) and name in {"plain", "thousands_comma"}:
            return ValueClass(CanonicalType.IDENTIFIER, f"id_{name}",
                              f"Identifier written as a number ({desc})")
        is_int = name in {"plain"} or (
            name in {"thousands_comma", "thousands_space"} and "." not in s
        )
        return ValueClass(
            CanonicalType.INTEGER if is_int else CanonicalType.DECIMAL, name, desc
        )
    if d:  # the excel-serial candidate, resolved here
        return d

    # boolean --------------------------------------------------------
    if low in TRUE_TOKENS or low in FALSE_TOKENS:
        return ValueClass(CanonicalType.BOOLEAN, "boolean_token",
                          "Boolean-like token (Yes/No, True/False)")

    # identifier -----------------------------------------------------
    for name, rx, desc in IDENTIFIER_PATTERNS:
        if rx.match(s):
            return ValueClass(CanonicalType.IDENTIFIER, name, desc)
    if ID_NAME_RE.search(column_name) and len(s) <= 40 and " " not in s:
        return ValueClass(CanonicalType.IDENTIFIER, "id_by_name",
                          "Treated as an identifier because of the column name")

    return ValueClass(CanonicalType.STRING, "text", "Free text")


# --------------------------------------------------------------------------
# Column-level aggregation
# --------------------------------------------------------------------------
@dataclass
class ColumnTypeVerdict:
    canonical: CanonicalType
    share: float
    pattern_counts: Counter[str] = field(default_factory=Counter)
    pattern_descriptions: dict[str, str] = field(default_factory=dict)
    pattern_examples: dict[str, list[str]] = field(default_factory=dict)
    type_counts: Counter[str] = field(default_factory=Counter)
    currencies: set[str] = field(default_factory=set)
    ambiguous_patterns: set[str] = field(default_factory=set)
    non_null: int = 0

    @property
    def is_mixed(self) -> bool:
        """More than one incompatible canonical type with a real share."""
        significant = [t for t, c in self.type_counts.items()
                       if self.non_null and c / self.non_null >= 0.05]
        return len(significant) > 1


#: Pairs/sets of types that can be unified without losing information.
_UNIFY: list[tuple[set[CanonicalType], CanonicalType]] = [
    ({CanonicalType.INTEGER, CanonicalType.DECIMAL}, CanonicalType.DECIMAL),
    ({CanonicalType.INTEGER, CanonicalType.CURRENCY}, CanonicalType.CURRENCY),
    ({CanonicalType.DECIMAL, CanonicalType.CURRENCY}, CanonicalType.CURRENCY),
    ({CanonicalType.INTEGER, CanonicalType.DECIMAL, CanonicalType.CURRENCY},
     CanonicalType.CURRENCY),
    ({CanonicalType.DATE, CanonicalType.DATETIME}, CanonicalType.DATETIME),
    ({CanonicalType.INTEGER, CanonicalType.IDENTIFIER}, CanonicalType.IDENTIFIER),
    ({CanonicalType.STRING, CanonicalType.IDENTIFIER}, CanonicalType.IDENTIFIER),
]


def classify_column(values: list[object], column_name: str = "") -> ColumnTypeVerdict:
    """Aggregate per-value classifications into a column verdict.

    The winning type is the one with the largest share of non-null values,
    after unifying compatible types (int+decimal -> decimal, etc.). Shares are
    reported so the caller can decide whether to trust it.
    """
    verdict = ColumnTypeVerdict(canonical=CanonicalType.STRING, share=0.0)
    for raw in values:
        vc = classify_value(raw, column_name)
        if vc is None:
            continue
        verdict.non_null += 1
        verdict.type_counts[vc.canonical.value] += 1
        verdict.pattern_counts[vc.pattern] += 1
        verdict.pattern_descriptions.setdefault(vc.pattern, vc.description)
        ex = verdict.pattern_examples.setdefault(vc.pattern, [])
        if len(ex) < 5:
            text = str(raw)
            if text not in ex:
                ex.append(text)
        if vc.currency:
            verdict.currencies.add(vc.currency)
        if vc.ambiguous:
            verdict.ambiguous_patterns.add(vc.pattern)

    if not verdict.non_null:
        verdict.canonical = CanonicalType.EMPTY
        return verdict

    present = {CanonicalType(t) for t in verdict.type_counts}
    if len(present) == 1:
        # One type throughout. The column may still carry several *surface
        # formats* (pattern_counts) -- that is reported separately and is what
        # "inconsistent date format" means.
        verdict.canonical = next(iter(present))
        verdict.share = 1.0
        return verdict
    for group, unified in _UNIFY:
        if present and present <= group:
            verdict.canonical = unified
            verdict.share = 1.0
            return verdict

    top, count = verdict.type_counts.most_common(1)[0]
    verdict.canonical = CanonicalType(top)
    verdict.share = count / verdict.non_null
    if verdict.is_mixed and verdict.share < 0.9:
        # Keep the dominant type as the proposal but mark the column mixed so
        # the prep agent plans an explicit parse step rather than casting.
        verdict.canonical = CanonicalType(top)
    return verdict


# --------------------------------------------------------------------------
# Naming conventions
# --------------------------------------------------------------------------
def naming_convention(names: list[str]) -> str:
    """Classify the convention of a set of headers, or 'mixed'."""
    def one(n: str) -> str:
        n = n.strip()
        if not n:
            return "empty"
        if re.fullmatch(r"[a-z0-9]+(_[a-z0-9]+)*", n):
            return "snake_case"
        if re.fullmatch(r"[A-Z0-9]+(_[A-Z0-9]+)*", n):
            return "UPPER_SNAKE"
        if re.fullmatch(r"[a-z]+([A-Z][a-z0-9]*)+", n):
            return "camelCase"
        if re.fullmatch(r"([A-Z][a-z0-9]*)+", n):
            return "PascalCase"
        if re.fullmatch(r"[A-Za-z0-9]+([ ][A-Za-z0-9]+)*", n) and " " in n:
            return "Title Case" if n[:1].isupper() else "lower case"
        if "-" in n:
            return "kebab-case"
        return "irregular"

    kinds = Counter(one(n) for n in names if n and not n.startswith("Unnamed"))
    if not kinds:
        return ""
    if len(kinds) == 1:
        return next(iter(kinds))
    top, n = kinds.most_common(1)[0]
    total = sum(kinds.values())
    return top if n / total >= 0.8 else "mixed"


def canonicalise_name(name: str) -> str:
    """Normalise a header for *comparison* (not for renaming): lowercase,
    strip punctuation and common decorations like units in parentheses."""
    n = str(name).strip().lower()
    n = re.sub(r"\(([^)]*)\)", r" \1 ", n)
    n = re.sub(r"[^a-z0-9]+", "_", n)
    n = re.sub(r"_+", "_", n).strip("_")
    # drop noise words that differ between systems but mean nothing
    n = re.sub(r"^(the|a)_", "", n)
    for noise in ("_amt", "_amount", "_value", "_val"):
        if n.endswith(noise) and len(n) > len(noise) + 2:
            n = n[: -len(noise)]
            break
    aliases = {
        "proj": "project", "prj": "project", "projno": "project_number",
        "dept": "department", "emp": "employee", "qty": "quantity",
        "desc": "description", "cust": "customer", "mgr": "manager",
        "hrs": "hours", "hr": "hours", "cat": "category",
    }
    parts = [aliases.get(p, p) for p in n.split("_") if p]
    # collapse duplicated tokens: project_project_id -> project_id
    out: list[str] = []
    for p in parts:
        if not out or out[-1] != p:
            out.append(p)
    return "_".join(out)


def name_similarity(a: str, b: str) -> float:
    """Token-overlap similarity on canonicalised names, 0..1."""
    ta = set(canonicalise_name(a).split("_"))
    tb = set(canonicalise_name(b).split("_"))
    ta.discard("")
    tb.discard("")
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
