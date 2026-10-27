"""A catalogue of DAX functions with arity and semantics.

Purpose: an LLM will happily emit ``SUMPRODUCT`` (Excel, not DAX) or
``TOTALYTDX`` (does not exist). Checking every function name against this
catalogue is what turns "the DAX looks right" into "the DAX references only
functions that exist, with the right number of arguments".

The catalogue is deliberately **not exhaustive** -- DAX has hundreds of
functions. It covers what the generator emits plus what a user is likely to
hand-write, and ``UNKNOWN_IS_ERROR`` controls the verdict for anything else:
an unlisted name is reported as *unrecognised* (a warning naming the
catalogue's limits), while a name on ``KNOWN_NOT_DAX`` is reported as a hard
error because we know it is wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FnCategory(str, Enum):
    AGGREGATION = "aggregation"
    ITERATOR = "iterator"
    FILTER = "filter"
    TIME_INTELLIGENCE = "time_intelligence"
    LOGICAL = "logical"
    MATH = "math"
    TEXT = "text"
    DATE = "date"
    INFO = "info"
    TABLE = "table"
    RELATIONSHIP = "relationship"
    STATISTICAL = "statistical"


@dataclass(frozen=True)
class DaxFunction:
    name: str
    category: FnCategory
    min_args: int
    max_args: int | None  # None = variadic
    #: Argument positions (0-based) that must be a column reference.
    column_args: tuple[int, ...] = ()
    #: Argument positions that must be a table expression.
    table_args: tuple[int, ...] = ()
    #: True when the function needs a marked date table to behave correctly.
    requires_date_table: bool = False
    #: True when the function introduces a row context (an iterator).
    creates_row_context: bool = False
    #: True when the function returns a table rather than a scalar.
    returns_table: bool = False
    note: str = ""


def _f(
    name: str, category: FnCategory, min_args: int, max_args: int | None, **kwargs: object
) -> DaxFunction:
    return DaxFunction(name=name.upper(), category=category, min_args=min_args,
                       max_args=max_args, **kwargs)  # type: ignore[arg-type]


_FUNCTIONS: tuple[DaxFunction, ...] = (
    # --- simple aggregations (take a single column) --------------------
    _f("SUM", FnCategory.AGGREGATION, 1, 1, column_args=(0,)),
    _f("AVERAGE", FnCategory.AGGREGATION, 1, 1, column_args=(0,)),
    _f("MIN", FnCategory.AGGREGATION, 1, 2),
    _f("MAX", FnCategory.AGGREGATION, 1, 2),
    _f("COUNT", FnCategory.AGGREGATION, 1, 1, column_args=(0,)),
    _f("COUNTA", FnCategory.AGGREGATION, 1, 1, column_args=(0,)),
    _f("COUNTBLANK", FnCategory.AGGREGATION, 1, 1, column_args=(0,)),
    _f("DISTINCTCOUNT", FnCategory.AGGREGATION, 1, 1, column_args=(0,)),
    _f("DISTINCTCOUNTNOBLANK", FnCategory.AGGREGATION, 1, 1, column_args=(0,)),
    _f("COUNTROWS", FnCategory.AGGREGATION, 0, 1, table_args=(0,)),
    _f("PRODUCT", FnCategory.AGGREGATION, 1, 1, column_args=(0,)),
    _f("MEDIAN", FnCategory.STATISTICAL, 1, 1, column_args=(0,)),
    _f("STDEV.P", FnCategory.STATISTICAL, 1, 1, column_args=(0,)),
    _f("STDEV.S", FnCategory.STATISTICAL, 1, 1, column_args=(0,)),
    _f("VAR.P", FnCategory.STATISTICAL, 1, 1, column_args=(0,)),
    _f("VAR.S", FnCategory.STATISTICAL, 1, 1, column_args=(0,)),
    _f("PERCENTILE.INC", FnCategory.STATISTICAL, 2, 2, column_args=(0,)),
    _f("PERCENTILE.EXC", FnCategory.STATISTICAL, 2, 2, column_args=(0,)),

    # --- iterators (table + expression; create row context) ------------
    _f("SUMX", FnCategory.ITERATOR, 2, 2, table_args=(0,), creates_row_context=True),
    _f("AVERAGEX", FnCategory.ITERATOR, 2, 2, table_args=(0,), creates_row_context=True),
    _f("MINX", FnCategory.ITERATOR, 2, 2, table_args=(0,), creates_row_context=True),
    _f("MAXX", FnCategory.ITERATOR, 2, 2, table_args=(0,), creates_row_context=True),
    _f("COUNTX", FnCategory.ITERATOR, 2, 2, table_args=(0,), creates_row_context=True),
    _f("COUNTAX", FnCategory.ITERATOR, 2, 2, table_args=(0,), creates_row_context=True),
    _f("PRODUCTX", FnCategory.ITERATOR, 2, 2, table_args=(0,), creates_row_context=True),
    _f("MEDIANX", FnCategory.ITERATOR, 2, 2, table_args=(0,), creates_row_context=True),
    _f("RANKX", FnCategory.ITERATOR, 2, 5, table_args=(0,), creates_row_context=True),
    _f("CONCATENATEX", FnCategory.ITERATOR, 2, None, table_args=(0,),
       creates_row_context=True),

    # --- filter / context ---------------------------------------------
    _f("CALCULATE", FnCategory.FILTER, 1, None,
       note="Performs context transition; a row context becomes a filter context."),
    _f("CALCULATETABLE", FnCategory.FILTER, 1, None, returns_table=True),
    _f("FILTER", FnCategory.FILTER, 2, 2, table_args=(0,), returns_table=True,
       creates_row_context=True),
    _f("ALL", FnCategory.FILTER, 0, None, returns_table=True),
    _f("ALLEXCEPT", FnCategory.FILTER, 2, None, table_args=(0,), returns_table=True),
    _f("ALLSELECTED", FnCategory.FILTER, 0, 1, returns_table=True),
    _f("ALLNOBLANKROW", FnCategory.FILTER, 1, None, returns_table=True),
    _f("REMOVEFILTERS", FnCategory.FILTER, 0, None, returns_table=True),
    _f("KEEPFILTERS", FnCategory.FILTER, 1, 1),
    _f("VALUES", FnCategory.FILTER, 1, 1, returns_table=True),
    _f("DISTINCT", FnCategory.FILTER, 1, 1, returns_table=True),
    _f("SELECTEDVALUE", FnCategory.FILTER, 1, 2, column_args=(0,)),
    _f("HASONEVALUE", FnCategory.INFO, 1, 1, column_args=(0,)),
    _f("HASONEFILTER", FnCategory.INFO, 1, 1, column_args=(0,)),
    _f("ISFILTERED", FnCategory.INFO, 1, 1),
    _f("ISCROSSFILTERED", FnCategory.INFO, 1, 1),
    _f("EARLIER", FnCategory.FILTER, 1, 2, column_args=(0,),
       note="Legacy row-context access; a VAR is clearer and less error-prone."),
    _f("USERELATIONSHIP", FnCategory.RELATIONSHIP, 2, 2, column_args=(0, 1),
       note="Only valid inside CALCULATE."),
    _f("CROSSFILTER", FnCategory.RELATIONSHIP, 3, 3, column_args=(0, 1)),
    _f("RELATED", FnCategory.RELATIONSHIP, 1, 1, column_args=(0,),
       note="Needs a row context and a many-to-one relationship path."),
    _f("RELATEDTABLE", FnCategory.RELATIONSHIP, 1, 1, returns_table=True),

    # --- time intelligence (all need a marked date table) --------------
    _f("TOTALYTD", FnCategory.TIME_INTELLIGENCE, 2, 4, column_args=(1,),
       requires_date_table=True),
    _f("TOTALQTD", FnCategory.TIME_INTELLIGENCE, 2, 4, column_args=(1,),
       requires_date_table=True),
    _f("TOTALMTD", FnCategory.TIME_INTELLIGENCE, 2, 4, column_args=(1,),
       requires_date_table=True),
    _f("DATESYTD", FnCategory.TIME_INTELLIGENCE, 1, 2, column_args=(0,), requires_date_table=True,
       returns_table=True),
    _f("DATESQTD", FnCategory.TIME_INTELLIGENCE, 1, 1, column_args=(0,), requires_date_table=True,
       returns_table=True),
    _f("DATESMTD", FnCategory.TIME_INTELLIGENCE, 1, 1, column_args=(0,), requires_date_table=True,
       returns_table=True),
    _f("DATEADD", FnCategory.TIME_INTELLIGENCE, 3, 3, column_args=(0,), requires_date_table=True,
       returns_table=True),
    _f("DATESINPERIOD", FnCategory.TIME_INTELLIGENCE, 4, 4, column_args=(0,), requires_date_table=True,
       returns_table=True),
    _f("DATESBETWEEN", FnCategory.TIME_INTELLIGENCE, 3, 3, column_args=(0,), requires_date_table=True,
       returns_table=True),
    _f("PARALLELPERIOD", FnCategory.TIME_INTELLIGENCE, 3, 3, column_args=(0,), requires_date_table=True,
       returns_table=True),
    _f("SAMEPERIODLASTYEAR", FnCategory.TIME_INTELLIGENCE, 1, 1,
       column_args=(0,), requires_date_table=True, returns_table=True),
    _f("PREVIOUSMONTH", FnCategory.TIME_INTELLIGENCE, 1, 1, column_args=(0,), requires_date_table=True,
       returns_table=True),
    _f("PREVIOUSQUARTER", FnCategory.TIME_INTELLIGENCE, 1, 1, column_args=(0,), requires_date_table=True,
       returns_table=True),
    _f("PREVIOUSYEAR", FnCategory.TIME_INTELLIGENCE, 1, 2, column_args=(0,), requires_date_table=True,
       returns_table=True),
    _f("PREVIOUSDAY", FnCategory.TIME_INTELLIGENCE, 1, 1, column_args=(0,), requires_date_table=True,
       returns_table=True),
    _f("NEXTMONTH", FnCategory.TIME_INTELLIGENCE, 1, 1, column_args=(0,), requires_date_table=True,
       returns_table=True),
    _f("NEXTYEAR", FnCategory.TIME_INTELLIGENCE, 1, 2, column_args=(0,), requires_date_table=True,
       returns_table=True),
    _f("ENDOFMONTH", FnCategory.TIME_INTELLIGENCE, 1, 1, column_args=(0,), requires_date_table=True),
    _f("STARTOFMONTH", FnCategory.TIME_INTELLIGENCE, 1, 1, column_args=(0,), requires_date_table=True),
    _f("ENDOFYEAR", FnCategory.TIME_INTELLIGENCE, 1, 2, column_args=(0,), requires_date_table=True),
    _f("STARTOFYEAR", FnCategory.TIME_INTELLIGENCE, 1, 2, column_args=(0,), requires_date_table=True),
    _f("FIRSTDATE", FnCategory.TIME_INTELLIGENCE, 1, 1, column_args=(0,)),
    _f("LASTDATE", FnCategory.TIME_INTELLIGENCE, 1, 1, column_args=(0,)),
    _f("CALENDAR", FnCategory.TABLE, 2, 2, returns_table=True),
    _f("CALENDARAUTO", FnCategory.TABLE, 0, 1, returns_table=True),

    # --- logical / info ------------------------------------------------
    _f("IF", FnCategory.LOGICAL, 2, 3),
    _f("IFERROR", FnCategory.LOGICAL, 2, 2),
    _f("SWITCH", FnCategory.LOGICAL, 3, None),
    _f("AND", FnCategory.LOGICAL, 2, 2, note="Only two arguments; use && for more."),
    _f("OR", FnCategory.LOGICAL, 2, 2, note="Only two arguments; use || for more."),
    _f("NOT", FnCategory.LOGICAL, 1, 1),
    _f("COALESCE", FnCategory.LOGICAL, 1, None),
    _f("ISBLANK", FnCategory.INFO, 1, 1),
    _f("ISERROR", FnCategory.INFO, 1, 1),
    _f("ISEMPTY", FnCategory.INFO, 1, 1, table_args=(0,)),
    _f("ISNUMBER", FnCategory.INFO, 1, 1),
    _f("ISTEXT", FnCategory.INFO, 1, 1),
    _f("ISINSCOPE", FnCategory.INFO, 1, 1, column_args=(0,)),
    _f("BLANK", FnCategory.INFO, 0, 0),
    _f("ERROR", FnCategory.INFO, 1, 1),

    # --- math ----------------------------------------------------------
    _f("DIVIDE", FnCategory.MATH, 2, 3,
       note="Safe division: returns BLANK (or the third argument) instead of an error."),
    _f("ABS", FnCategory.MATH, 1, 1),
    _f("ROUND", FnCategory.MATH, 2, 2),
    _f("ROUNDUP", FnCategory.MATH, 2, 2),
    _f("ROUNDDOWN", FnCategory.MATH, 2, 2),
    _f("INT", FnCategory.MATH, 1, 1),
    _f("TRUNC", FnCategory.MATH, 1, 2),
    _f("CEILING", FnCategory.MATH, 2, 2),
    _f("FLOOR", FnCategory.MATH, 2, 2),
    _f("MROUND", FnCategory.MATH, 2, 2),
    _f("POWER", FnCategory.MATH, 2, 2),
    _f("SQRT", FnCategory.MATH, 1, 1),
    _f("EXP", FnCategory.MATH, 1, 1),
    _f("LN", FnCategory.MATH, 1, 1),
    _f("LOG", FnCategory.MATH, 1, 2),
    _f("LOG10", FnCategory.MATH, 1, 1),
    _f("SIGN", FnCategory.MATH, 1, 1),
    _f("MOD", FnCategory.MATH, 2, 2),

    # --- text ----------------------------------------------------------
    _f("CONCATENATE", FnCategory.TEXT, 2, 2),
    _f("FORMAT", FnCategory.TEXT, 2, 3),
    _f("LEFT", FnCategory.TEXT, 1, 2),
    _f("RIGHT", FnCategory.TEXT, 1, 2),
    _f("MID", FnCategory.TEXT, 3, 3),
    _f("LEN", FnCategory.TEXT, 1, 1),
    _f("LOWER", FnCategory.TEXT, 1, 1),
    _f("UPPER", FnCategory.TEXT, 1, 1),
    _f("TRIM", FnCategory.TEXT, 1, 1),
    _f("SUBSTITUTE", FnCategory.TEXT, 3, 4),
    _f("SEARCH", FnCategory.TEXT, 2, 4),
    _f("FIND", FnCategory.TEXT, 2, 4),
    _f("VALUE", FnCategory.TEXT, 1, 1),
    _f("UNICHAR", FnCategory.TEXT, 1, 1),

    # --- date ----------------------------------------------------------
    _f("DATE", FnCategory.DATE, 3, 3),
    _f("YEAR", FnCategory.DATE, 1, 1),
    _f("MONTH", FnCategory.DATE, 1, 1),
    _f("DAY", FnCategory.DATE, 1, 1),
    _f("QUARTER", FnCategory.DATE, 1, 1),
    _f("WEEKNUM", FnCategory.DATE, 1, 2),
    _f("WEEKDAY", FnCategory.DATE, 1, 2),
    _f("TODAY", FnCategory.DATE, 0, 0),
    _f("NOW", FnCategory.DATE, 0, 0),
    _f("EOMONTH", FnCategory.DATE, 2, 2),
    _f("EDATE", FnCategory.DATE, 2, 2),
    _f("DATEDIFF", FnCategory.DATE, 3, 3),
    _f("DATEVALUE", FnCategory.DATE, 1, 1),

    # --- table constructors -------------------------------------------
    _f("SUMMARIZE", FnCategory.TABLE, 2, None, table_args=(0,), returns_table=True),
    _f("SUMMARIZECOLUMNS", FnCategory.TABLE, 1, None, returns_table=True),
    _f("ADDCOLUMNS", FnCategory.TABLE, 3, None, table_args=(0,), returns_table=True,
       creates_row_context=True),
    _f("SELECTCOLUMNS", FnCategory.TABLE, 3, None, table_args=(0,), returns_table=True,
       creates_row_context=True),
    _f("ROW", FnCategory.TABLE, 2, None, returns_table=True),
    _f("UNION", FnCategory.TABLE, 2, None, returns_table=True),
    _f("INTERSECT", FnCategory.TABLE, 2, 2, returns_table=True),
    _f("EXCEPT", FnCategory.TABLE, 2, 2, returns_table=True),
    _f("NATURALINNERJOIN", FnCategory.TABLE, 2, 2, returns_table=True),
    _f("NATURALLEFTOUTERJOIN", FnCategory.TABLE, 2, 2, returns_table=True),
    _f("CROSSJOIN", FnCategory.TABLE, 2, None, returns_table=True),
    _f("TOPN", FnCategory.TABLE, 2, None, table_args=(1,), returns_table=True),
    _f("GENERATE", FnCategory.TABLE, 2, 2, returns_table=True),
    _f("GENERATEALL", FnCategory.TABLE, 2, 2, returns_table=True),
    _f("TREATAS", FnCategory.TABLE, 2, None, returns_table=True),
    _f("EXCEPTALL", FnCategory.TABLE, 2, 2, returns_table=True),
    _f("GROUPBY", FnCategory.TABLE, 1, None, table_args=(0,), returns_table=True),
    _f("CURRENTGROUP", FnCategory.TABLE, 0, 0, returns_table=True),
    _f("LOOKUPVALUE", FnCategory.RELATIONSHIP, 3, None, column_args=(0, 1),
       note="Use a relationship where one exists; LOOKUPVALUE does not benefit from it."),
    _f("CONTAINS", FnCategory.INFO, 3, None, table_args=(0,)),
    _f("CONTAINSROW", FnCategory.INFO, 2, None),
    _f("CONTAINSSTRING", FnCategory.TEXT, 2, 2),
    _f("IN", FnCategory.LOGICAL, 2, 2, note="Written as an infix operator: x IN {a, b}."),
    _f("USERPRINCIPALNAME", FnCategory.INFO, 0, 0),
    _f("CONVERT", FnCategory.MATH, 2, 2),
    _f("CURRENCY", FnCategory.MATH, 1, 1),
)

CATALOGUE: dict[str, DaxFunction] = {f.name: f for f in _FUNCTIONS}

#: Names that are definitely NOT DAX -- usually Excel, SQL or invented. These
#: produce a hard error rather than an "unrecognised" warning, with the right
#: alternative named.
KNOWN_NOT_DAX: dict[str, str] = {
    "SUMPRODUCT": "Excel only. Use SUMX(table, expr1 * expr2).",
    "SUMIF": "Excel only. Use CALCULATE(SUM(col), filter).",
    "SUMIFS": "Excel only. Use CALCULATE(SUM(col), filter1, filter2).",
    "COUNTIF": "Excel only. Use CALCULATE(COUNTROWS(table), filter).",
    "COUNTIFS": "Excel only. Use CALCULATE(COUNTROWS(table), filters).",
    "AVERAGEIF": "Excel only. Use CALCULATE(AVERAGE(col), filter).",
    "VLOOKUP": "Excel only. Use RELATED or LOOKUPVALUE.",
    "HLOOKUP": "Excel only. There is no DAX equivalent; model a relationship.",
    "INDEX": "Excel only in this sense. Use a relationship or LOOKUPVALUE.",
    "MATCH": "Excel only. Use a relationship or LOOKUPVALUE.",
    "IFS": "Excel only. Use SWITCH(TRUE(), cond1, val1, ...).",
    "TEXTJOIN": "Excel only. Use CONCATENATEX.",
    "CASE": "SQL only. Use SWITCH or IF.",
    "ISNULL": "SQL only. Use ISBLANK.",
    "NVL": "SQL only. Use COALESCE or IF(ISBLANK(x), y, x).",
    "COUNT_DISTINCT": "Not DAX. Use DISTINCTCOUNT.",
    "COUNTDISTINCT": "Not DAX. Use DISTINCTCOUNT.",
    "SUM_IF": "Not DAX. Use CALCULATE(SUM(col), filter).",
    "TOTALYTDX": "Does not exist. Use TOTALYTD.",
    "YTD": "Does not exist. Use TOTALYTD or DATESYTD.",
    "MTD": "Does not exist. Use TOTALMTD or DATESMTD.",
    "PREVIOUSPERIOD": "Does not exist. Use PARALLELPERIOD or DATEADD.",
    "SAMEPERIODPREVIOUSYEAR": "Misspelling. Use SAMEPERIODLASTYEAR.",
    "DISTINCTCOUNTX": "Does not exist. Use COUNTROWS(DISTINCT(col)).",
    "AVG": "Not DAX. Use AVERAGE.",
    "STDEV": "Ambiguous. Use STDEV.P or STDEV.S.",
    "VARIANCE": "Not DAX. Use VAR.P or VAR.S.",
    "GREATEST": "SQL only. Use MAX or MAXX.",
    "LEAST": "SQL only. Use MIN or MINX.",
}

#: Functions whose misuse is a common, consequential modelling error.
USAGE_WARNINGS: dict[str, str] = {
    "FILTER": "FILTER returns a table and iterates row by row. Inside CALCULATE a "
              "simple boolean predicate is faster and clearer unless the condition "
              "spans tables or needs a measure.",
    "EARLIER": "EARLIER is legacy. A VAR captured before the inner iteration is "
               "clearer and avoids nesting mistakes.",
    "ALL": "ALL removes every filter including the ones the user set on the page. "
           "ALLSELECTED or REMOVEFILTERS on named columns is usually intended.",
    "AND": "AND takes exactly two arguments. For three or more conditions use &&.",
    "OR": "OR takes exactly two arguments. For three or more conditions use ||.",
}


def lookup(name: str) -> DaxFunction | None:
    return CATALOGUE.get(name.upper())


def is_known_wrong(name: str) -> str:
    return KNOWN_NOT_DAX.get(name.upper(), "")


def suggest(name: str) -> str:
    """Nearest catalogue entry, for an 'did you mean' hint."""
    import difflib

    matches = difflib.get_close_matches(name.upper(), list(CATALOGUE), n=1, cutoff=0.78)
    return matches[0] if matches else ""


def time_intelligence_names() -> set[str]:
    return {f.name for f in _FUNCTIONS if f.requires_date_table}
