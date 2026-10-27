"""Generating Power Query M for an executed plan.

Why generate M at all, when the data is already transformed in pandas? Because
the deliverable is an *editable* Power BI project. If the PBIP shipped a
pre-transformed CSV, the user could not see or change how the data was
cleaned. Emitting M means the semantic model's partitions carry the real
transformation logic, so a Power BI developer can open the query, read it, and
edit it.

Each ``TransformOp`` has an emitter here; ``test_every_op_has_handler_and_m_emitter``
asserts the mapping stays complete. An op with no faithful M equivalent emits a
clearly-worded comment saying what was done outside Power Query and why,
rather than silently producing M that does something different.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...schemas.prep import TransformOp, TransformationStep

Emitter = Callable[[TransformationStep, str], tuple[str, str]]

#: Power Query identifiers that must be quoted in #"..." form.
def ident(name: str) -> str:
    return f'#"{name}"'


def mstr(value: Any) -> str:
    """A literal as M source."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace('"', '""')
    return f'"{escaped}"'


def mlist(values: list[Any]) -> str:
    return "{" + ", ".join(mstr(v) for v in values) + "}"


def _step_name(step: TransformationStep) -> str:
    base = step.op.value.replace("_", " ").title().replace(" ", "")
    return f"{base}{step.ordinal}"


# --------------------------------------------------------------------------
# Emitters: (step, previous_step_variable) -> (new_variable, m_expression)
# --------------------------------------------------------------------------
def e_rename(step: TransformationStep, prev: str) -> tuple[str, str]:
    pairs = ", ".join(
        f"{{{mstr(src)}, {mstr(dst)}}}" for src, dst in step.params["renames"].items()
    )
    return _step_name(step), (
        f"Table.RenameColumns({prev}, {{{pairs}}}, MissingField.Ignore)"
    )


def e_select(step: TransformationStep, prev: str) -> tuple[str, str]:
    return _step_name(step), (
        f"Table.SelectColumns({prev}, {mlist(step.params['columns'])}, MissingField.Ignore)"
    )


def e_drop(step: TransformationStep, prev: str) -> tuple[str, str]:
    return _step_name(step), (
        f"Table.RemoveColumns({prev}, {mlist(step.params['columns'])}, MissingField.Ignore)"
    )


def e_trim(step: TransformationStep, prev: str) -> tuple[str, str]:
    col = step.params["column"]
    # Text.Trim handles the ends; Text.Combine/Text.Split collapses runs.
    return _step_name(step), (
        f"Table.TransformColumns({prev}, {{{{{mstr(col)}, each if _ = null then null else "
        f"Text.Combine(List.Select(Text.Split(Text.Trim(Text.From(_)), \" \"), "
        f"each _ <> \"\"), \" \"), type text}}}})"
    )


def e_normalise_case(step: TransformationStep, prev: str) -> tuple[str, str]:
    col = step.params["column"]
    fn = {"upper": "Text.Upper", "lower": "Text.Lower", "title": "Text.Proper"}.get(
        step.params.get("mode", "title"), "Text.Proper"
    )
    return _step_name(step), (
        f"Table.TransformColumns({prev}, {{{{{mstr(col)}, each if _ = null then null "
        f"else {fn}(Text.From(_)), type text}}}})"
    )


def e_normalise_identifier(step: TransformationStep, prev: str) -> tuple[str, str]:
    col = step.params["column"]
    name = _step_name(step)
    # Keep the original, then normalise: trim, remove separators, upper-case,
    # and strip leading zeros from the numeric tail.
    expr = (
        f"let\n"
        f"        Keep = Table.AddColumn({prev}, {mstr(col + '_original')}, "
        f"each Record.Field(_, {mstr(col)}), type text),\n"
        f"        Normalised = Table.TransformColumns(Keep, {{{{{mstr(col)}, each\n"
        f"            if _ = null then null else\n"
        f"            let\n"
        f"                Raw   = Text.Upper(Text.Trim(Text.From(_))),\n"
        f"                NoSep = Text.Remove(Raw, {{\" \", \"-\", \"_\", \".\", \"/\"}}),\n"
        f"                Alpha = Text.Select(NoSep, {{\"A\".\"Z\"}}),\n"
        f"                Digits = Text.Select(NoSep, {{\"0\".\"9\"}}),\n"
        f"                Trimmed = if Digits = \"\" then \"\" else "
        f"Text.TrimStart(Digits, \"0\"),\n"
        f"                Rebuilt = Alpha & (if Trimmed = \"\" and Digits <> \"\" "
        f"then \"0\" else Trimmed)\n"
        f"            in\n"
        f"                if Alpha = \"\" and Digits = \"\" then NoSep else Rebuilt,\n"
        f"            type text}}}})\n"
        f"    in\n"
        f"        Normalised"
    )
    return name, expr


def e_parse_date(step: TransformationStep, prev: str) -> tuple[str, str]:
    col = step.params["column"]
    formats = [f.get("pattern", "") for f in step.params.get("formats", [])]
    dayfirst = bool(step.params.get("dayfirst_default", True))
    culture = "en-GB" if dayfirst else "en-US"
    name = _step_name(step)
    comment = (
        f"    // Source formats detected: {', '.join(formats) or 'single format'}.\n"
        f"    // Parsed with the {culture} culture so dd/mm is read day-first.\n"
        f"    // Values that will not parse are kept in "
        f"{col}_unparsed rather than becoming null.\n"
    )
    expr = (
        f"let\n"
        f"        Quarantine = Table.AddColumn({prev}, {mstr(col + '_unparsed')}, each\n"
        f"            if Record.Field(_, {mstr(col)}) = null then null\n"
        f"            else if (try Date.From(Text.From(Record.Field(_, {mstr(col)})), "
        f'"{culture}") otherwise null) = null\n'
        f"                 then Text.From(Record.Field(_, {mstr(col)})) else null, "
        f"type nullable text),\n"
        f"        Parsed = Table.TransformColumns(Quarantine, {{{{{mstr(col)}, each\n"
        f"            if _ = null then null else "
        f'try Date.From(Text.From(_), "{culture}") otherwise null,\n'
        f"            type nullable date}}}})\n"
        f"    in\n"
        f"        Parsed"
    )
    return name, comment.rstrip("\n") + "\n    " + expr


def _m_number_cleanup(col: str) -> str:
    """M that turns a business-formatted amount into a number.

    Handles currency symbols and codes, thousands separators, accounting
    parentheses, trailing minus and the European decimal comma -- the same
    cases the pandas parser handles, so the two agree.
    """
    return (
        f"let\n"
        f"                Txt0 = Text.Upper(Text.Trim(Text.From(_))),\n"
        f"                Neg  = Text.StartsWith(Txt0, \"(\") or Text.EndsWith(Txt0, \"-\"),\n"
        f"                Txt1 = Text.Remove(Txt0, {{\"(\", \")\", \"$\", \"£\", \"€\", "
        f"\"¥\", \"₹\", \" \"}}),\n"
        f"                Txt2 = Text.Remove(Text.Replace(Text.Replace(Text.Replace("
        f"Txt1, \"USD\", \"\"), \"EUR\", \"\"), \"GBP\", \"\"), {{\"A\".\"Z\"}}),\n"
        f"                LastDot = Text.PositionOf(Txt2, \".\", Occurrence.Last),\n"
        f"                LastCom = Text.PositionOf(Txt2, \",\", Occurrence.Last),\n"
        f"                Txt3 =\n"
        f"                    if LastDot >= 0 and LastCom >= 0 then\n"
        f"                        if LastDot > LastCom\n"
        f"                            then Text.Remove(Txt2, {{\",\"}})\n"
        f"                            else Text.Replace(Text.Remove(Txt2, {{\".\"}}), "
        f"\",\", \".\")\n"
        f"                    else if LastCom >= 0 then\n"
        f"                        if Text.Length(Txt2) - LastCom - 1 <= 2\n"
        f"                            then Text.Replace(Txt2, \",\", \".\")\n"
        f"                            else Text.Remove(Txt2, {{\",\"}})\n"
        f"                    else Txt2,\n"
        f"                Txt4 = Text.Replace(Txt3, \"-\", \"\"),\n"
        f"                Num  = try Number.FromText(Txt4) otherwise null\n"
        f"            in\n"
        f"                if Num = null then null else (if Neg then -Num else Num)"
    )


def e_parse_number(step: TransformationStep, prev: str) -> tuple[str, str]:
    col = step.params["column"]
    name = _step_name(step)
    if step.params.get("unit_normalisation") == "hours":
        comment = (
            "    // Hours arrive as decimals, as a unit suffix (7.5h, 450min) or as\n"
            "    // hh:mm. All three are converted to decimal hours, and the original\n"
            f"    // surface format is recorded in {col}_source_format.\n"
        )
        expr = (
            f"let\n"
            f"        Audit = Table.AddColumn({prev}, {mstr(col + '_source_format')}, each\n"
            f"            let T = Text.Upper(Text.From(Record.Field(_, {mstr(col)}))) in\n"
            f"            if Record.Field(_, {mstr(col)}) = null then \"\"\n"
            f"            else if Text.Contains(T, \":\") then \"duration_hhmm\"\n"
            f"            else if Text.Contains(T, \"MIN\") then \"minutes\"\n"
            f"            else if Text.Contains(T, \"H\") then \"duration_unit\"\n"
            f"            else \"decimal\", type text),\n"
            f"        Hours = Table.TransformColumns(Audit, {{{{{mstr(col)}, each\n"
            f"            if _ = null then null else\n"
            f"            let\n"
            f"                T = Text.Upper(Text.Trim(Text.From(_))),\n"
            f"                Parts = Text.Split(T, \":\")\n"
            f"            in\n"
            f"                if Text.Contains(T, \":\") and List.Count(Parts) >= 2 then\n"
            f"                    (try Number.FromText(Parts{{0}}) otherwise 0)\n"
            f"                      + (try Number.FromText(Parts{{1}}) otherwise 0) / 60\n"
            f"                else if Text.Contains(T, \"MIN\") then\n"
            f"                    (try Number.FromText(Text.Select(T, {{\"0\".\"9\", \".\"}}))"
            f" otherwise null) / 60\n"
            f"                else\n"
            f"                    try Number.FromText(Text.Select(T, {{\"0\".\"9\", \".\", "
            f"\"-\"}})) otherwise null,\n"
            f"            type nullable number}}}})\n"
            f"    in\n"
            f"        Hours"
        )
        return name, comment.rstrip("\n") + "\n    " + expr
    expr = (
        f"Table.TransformColumns({prev}, {{{{{mstr(col)}, each\n"
        f"            if _ = null then null else {_m_number_cleanup(col)},\n"
        f"        type nullable number}}}})"
    )
    return name, expr


def e_parse_currency(step: TransformationStep, prev: str) -> tuple[str, str]:
    col = step.params["column"]
    currency_col = step.params.get("currency_column") or f"{col}_currency"
    expected = step.params.get("expected_currency") or ""
    name = _step_name(step)
    comment = (
        f"    // The currency is detected per row into {currency_col} BEFORE the symbol\n"
        f"    // is stripped. Rows not in the reporting currency"
        + (f" ({expected})" if expected else "")
        + " are marked so a\n    // single-currency total can exclude them "
          "instead of adding unlike units.\n"
    )
    expr = (
        f"let\n"
        f"        WithCurrency = Table.AddColumn({prev}, {mstr(currency_col)}, each\n"
        f"            let T = Text.Upper(Text.From(Record.Field(_, {mstr(col)}))) in\n"
        f"            if Record.Field(_, {mstr(col)}) = null then null\n"
        f"            else if Text.Contains(T, \"$\") or Text.Contains(T, \"USD\") "
        f"then \"USD\"\n"
        f"            else if Text.Contains(T, \"€\") or Text.Contains(T, \"EUR\") "
        f"then \"EUR\"\n"
        f"            else if Text.Contains(T, \"£\") or Text.Contains(T, \"GBP\") "
        f"then \"GBP\"\n"
        f"            else {mstr(expected) if expected else 'null'}, type nullable text),\n"
        f"        Amount = Table.TransformColumns(WithCurrency, {{{{{mstr(col)}, each\n"
        f"            if _ = null then null else {_m_number_cleanup(col)},\n"
        f"            type nullable number}}}})"
        + (
            f",\n        Flagged = Table.AddColumn(Amount, "
            f"{mstr(col + '_is_reporting_currency')}, each\n"
            f"            Record.Field(_, {mstr(currency_col)}) = {mstr(expected)}, "
            f"type logical)\n    in\n        Flagged"
            if expected else "\n    in\n        Amount"
        )
    )
    return name, comment.rstrip("\n") + "\n    " + expr


def e_parse_percentage(step: TransformationStep, prev: str) -> tuple[str, str]:
    col = step.params["column"]
    return _step_name(step), (
        f"Table.TransformColumns({prev}, {{{{{mstr(col)}, each\n"
        f"            if _ = null then null else\n"
        f"            let T = Text.Trim(Text.From(_)),\n"
        f"                HasPct = Text.Contains(T, \"%\"),\n"
        f"                N = try Number.FromText(Text.Remove(T, {{\"%\", \" \", \",\"}})) "
        f"otherwise null\n"
        f"            in if N = null then null else (if HasPct then N / 100 else N),\n"
        f"        Percentage.Type}}}})"
    )


def e_cast(step: TransformationStep, prev: str) -> tuple[str, str]:
    from ...schemas.common import CanonicalType
    col = step.params["column"]
    target = CanonicalType(step.params["to"])
    m_type = {
        CanonicalType.INTEGER: "Int64.Type", CanonicalType.DECIMAL: "type number",
        CanonicalType.CURRENCY: "Currency.Type", CanonicalType.PERCENTAGE: "Percentage.Type",
        CanonicalType.DATE: "type date", CanonicalType.DATETIME: "type datetime",
        CanonicalType.BOOLEAN: "type logical",
    }.get(target, "type text")
    return _step_name(step), (
        f"Table.TransformColumnTypes({prev}, {{{{{mstr(col)}, {m_type}}}}})"
    )


def e_map_category(step: TransformationStep, prev: str) -> tuple[str, str]:
    col = step.params["column"]
    mapping: dict[str, str] = step.params.get("mapping") or {}
    if not mapping:
        return _step_name(step), (
            f"// No vocabulary mapping has been confirmed for {col}, so no value is\n"
            f"    // changed here. Supply the mapping and re-generate to apply it.\n"
            f"    {prev}"
        )
    pairs = ", ".join(f"{{{mstr(k)}, {mstr(v)}}}" for k, v in mapping.items())
    return _step_name(step), (
        f"Table.TransformColumns({prev}, {{{{{mstr(col)}, each\n"
        f"            let Lookup = List.Select({{{pairs}}}, (p) => p{{0}} = _) in\n"
        f"            if List.IsEmpty(Lookup) then _ else Lookup{{0}}{{1}},\n"
        f"        type text}}}})"
    )


def e_fill_missing(step: TransformationStep, prev: str) -> tuple[str, str]:
    col, value = step.params["column"], step.params["value"]
    flag = step.params.get("flag_column")
    name = _step_name(step)
    if flag:
        return name, (
            f"let\n"
            f"        Flag = Table.AddColumn({prev}, {mstr(flag)}, each\n"
            f"            Record.Field(_, {mstr(col)}) = null or "
            f"Text.Trim(Text.From(Record.Field(_, {mstr(col)})) ?? \"\") = \"\", "
            f"type logical),\n"
            f"        Filled = Table.ReplaceValue(Flag, each Record.Field(_, {mstr(col)}),\n"
            f"            each if Record.Field(_, {mstr(col)}) = null or "
            f"Text.Trim(Text.From(Record.Field(_, {mstr(col)})) ?? \"\") = \"\"\n"
            f"                 then {mstr(value)} else Record.Field(_, {mstr(col)}),\n"
            f"            Replacer.ReplaceValue, {{{mstr(col)}}})\n"
            f"    in\n"
            f"        Filled"
        )
    return name, (
        f"Table.ReplaceValue({prev}, null, {mstr(value)}, Replacer.ReplaceValue, "
        f"{{{mstr(col)}}})"
    )


def e_flag_missing(step: TransformationStep, prev: str) -> tuple[str, str]:
    col = step.params["column"]
    flag = step.params.get("flag_column") or f"{col}_is_missing"
    return _step_name(step), (
        f"Table.AddColumn({prev}, {mstr(flag)}, each "
        f"Record.Field(_, {mstr(col)}) = null, type logical)"
    )


def e_remove_empty_rows(step: TransformationStep, prev: str) -> tuple[str, str]:
    return _step_name(step), (
        f"Table.SelectRows({prev}, each not List.IsEmpty(List.RemoveMatchingItems("
        f"Record.FieldValues(_), {{\"\", null}})))"
    )


def e_deduplicate(step: TransformationStep, prev: str) -> tuple[str, str]:
    subset = step.params.get("subset")
    if subset:
        return _step_name(step), (
            f"Table.Distinct({prev}, {mlist(subset)})"
        )
    return _step_name(step), f"Table.Distinct({prev})"


def e_surrogate_key(step: TransformationStep, prev: str) -> tuple[str, str]:
    name_col = step.params.get("column", "surrogate_key")
    return _step_name(step), (
        f"Table.AddIndexColumn({prev}, {mstr(name_col)}, 1, 1, Int64.Type)"
    )


def e_computed(step: TransformationStep, prev: str) -> tuple[str, str]:
    p = step.params
    col, kind = p["column"], p["kind"]
    if kind == "multiply":
        body = (f"Record.Field(_, {mstr(p['left'])}) * Record.Field(_, {mstr(p['right'])})")
        t = "type number"
    elif kind == "subtract":
        body = (f"Record.Field(_, {mstr(p['left'])}) - Record.Field(_, {mstr(p['right'])})")
        t = "type number"
    elif kind == "year":
        body = f"Date.Year(Record.Field(_, {mstr(p['source'])}))"
        t = "Int64.Type"
    elif kind == "month_start":
        body = f"Date.StartOfMonth(Record.Field(_, {mstr(p['source'])}))"
        t = "type date"
    elif kind == "constant":
        body = mstr(p.get("value"))
        t = "type any"
    elif kind == "concat":
        inner = ", ".join(
            f"Text.From(Record.Field(_, {mstr(s)}))" for s in p["sources"]
        )
        body = f"Text.Combine({{{inner}}}, {mstr(p.get('separator', '-'))})"
        t = "type text"
    else:
        return _step_name(step), (
            f"// Unsupported computation '{kind}'; no M emitted.\n    {prev}"
        )
    return _step_name(step), f"Table.AddColumn({prev}, {mstr(col)}, each {body}, {t})"


def e_split(step: TransformationStep, prev: str) -> tuple[str, str]:
    col = step.params["column"]
    into = step.params["into"]
    sep = step.params.get("separator", " ")
    return _step_name(step), (
        f"Table.SplitColumn({prev}, {mstr(col)}, "
        f"Splitter.SplitTextByDelimiter({mstr(sep)}, QuoteStyle.Csv), {mlist(into)})"
    )


def e_unpivot(step: TransformationStep, prev: str) -> tuple[str, str]:
    return _step_name(step), (
        f"Table.UnpivotOtherColumns({prev}, {mlist(step.params['id_columns'])}, "
        f"{mstr(step.params.get('name_column', 'attribute'))}, "
        f"{mstr(step.params.get('value_column', 'value'))})"
    )


def e_filter(step: TransformationStep, prev: str) -> tuple[str, str]:
    col, op = step.params["column"], step.params["op"]
    value = step.params.get("value")
    field = f"Record.Field(_, {mstr(col)})"
    condition = {
        "not_null": f"{field} <> null",
        "equals": f"{field} = {mstr(value)}",
        "not_equals": f"{field} <> {mstr(value)}",
        "in": f"List.Contains({mlist(value or [])}, {field})",
        "not_in": f"not List.Contains({mlist(value or [])}, {field})",
        "gte": f"{field} >= {mstr(value)}",
        "lte": f"{field} <= {mstr(value)}",
        "gt": f"{field} > {mstr(value)}",
        "lt": f"{field} < {mstr(value)}",
    }.get(op)
    if op == "between" and isinstance(value, (list, tuple)) and len(value) == 2:
        condition = f"{field} >= {mstr(value[0])} and {field} <= {mstr(value[1])}"
    if condition is None:
        return _step_name(step), f"// Unsupported filter '{op}'; no M emitted.\n    {prev}"
    return _step_name(step), f"Table.SelectRows({prev}, each {condition})"


def e_append(step: TransformationStep, prev: str) -> tuple[str, str]:
    tables = step.params["tables"]
    marker = step.params.get("source_column", "source_table")
    tagged = ",\n        ".join(
        f"Tagged{i} = Table.AddColumn({ident(t)}, {mstr(marker)}, each {mstr(t)}, type text)"
        for i, t in enumerate(tables)
    )
    names = ", ".join(f"Tagged{i}" for i in range(len(tables)))
    return _step_name(step), (
        f"let\n"
        f"        {tagged},\n"
        f"        Combined = Table.Combine({{{names}}})\n"
        f"    in\n"
        f"        Combined"
    )


def e_join(step: TransformationStep, prev: str) -> tuple[str, str]:
    p = step.params
    kind = {
        "left": "JoinKind.LeftOuter", "inner": "JoinKind.Inner",
        "full": "JoinKind.FullOuter",
    }.get(p.get("kind", "left"), "JoinKind.LeftOuter")
    right = p["right_table"]
    return _step_name(step), (
        f"let\n"
        f"        Joined = Table.NestedJoin({prev}, {mlist(p['left_keys'])}, "
        f"{ident(right)}, {mlist(p['right_keys'])}, \"{right}_rows\", {kind}),\n"
        f"        Expanded = Table.ExpandTableColumn(Joined, \"{right}_rows\",\n"
        f"            List.Difference(Table.ColumnNames({ident(right)}), "
        f"{mlist(p['right_keys'])}))\n"
        f"    in\n"
        f"        Expanded"
    )


def e_aggregate(step: TransformationStep, prev: str) -> tuple[str, str]:
    group = step.params["group_by"]
    aggs = step.params["aggregations"]
    fn = {
        "sum": "List.Sum", "mean": "List.Average", "min": "List.Min", "max": "List.Max",
        "count": "Table.RowCount", "nunique": "List.Count", "median": "List.Median",
    }
    specs = []
    for col, how in aggs.items():
        f = fn.get(how, "List.Sum")
        if how == "count":
            specs.append(f"{{{mstr(col)}, each Table.RowCount(_), Int64.Type}}")
        elif how == "nunique":
            specs.append(
                f"{{{mstr(col)}, each List.Count(List.Distinct("
                f"Table.Column(_, {mstr(col)}))), Int64.Type}}"
            )
        else:
            specs.append(
                f"{{{mstr(col)}, each {f}(Table.Column(_, {mstr(col)})), type number}}"
            )
    return _step_name(step), (
        f"Table.Group({prev}, {mlist(group)}, {{{', '.join(specs)}}})"
    )


def e_date_table(step: TransformationStep, prev: str) -> tuple[str, str]:
    p = step.params
    start, end = p["start"], p["end"]
    fiscal = int(p.get("fiscal_year_start_month", 1))
    return _step_name(step), (
        f"// A contiguous calendar is required for time intelligence. Mark this table\n"
        f"    // as the date table in Power BI Desktop (Table tools > Mark as date table,\n"
        f"    // using the 'date' column) -- that step cannot be set from TMDL alone.\n"
        f"    let\n"
        f"        StartDate = #date({start[:4]}, {int(start[5:7])}, {int(start[8:10])}),\n"
        f"        EndDate   = #date({end[:4]}, {int(end[5:7])}, {int(end[8:10])}),\n"
        f"        DayCount  = Duration.Days(EndDate - StartDate) + 1,\n"
        f"        Dates     = List.Dates(StartDate, DayCount, #duration(1, 0, 0, 0)),\n"
        f"        AsTable   = Table.FromList(Dates, Splitter.SplitByNothing(), "
        f"{{\"date\"}}),\n"
        f"        Typed     = Table.TransformColumnTypes(AsTable, "
        f"{{{{\"date\", type date}}}}),\n"
        f"        Expanded  = Table.AddColumn(\n"
        f"            Table.AddColumn(\n"
        f"              Table.AddColumn(\n"
        f"                Table.AddColumn(\n"
        f"                  Table.AddColumn(\n"
        f"                    Table.AddColumn(Typed, \"year\", each Date.Year([date]), "
        f"Int64.Type),\n"
        f"                    \"quarter\", each Date.QuarterOfYear([date]), Int64.Type),\n"
        f"                  \"month_number\", each Date.Month([date]), Int64.Type),\n"
        f"                \"month_name\", each Date.MonthName([date]), type text),\n"
        f"              \"month_year\", each Date.ToText([date], \"MMM yyyy\"), type text),\n"
        f"            \"month_key\", each Date.Year([date]) * 100 + Date.Month([date]), "
        f"Int64.Type),\n"
        f"        WithFiscal = Table.AddColumn(Expanded, \"fiscal_year\", each\n"
        f"            if Date.Month([date]) >= {fiscal} then Date.Year([date]) "
        f"+ {0 if fiscal == 1 else 1}\n"
        f"            else Date.Year([date]), Int64.Type),\n"
        f"        WithKey = Table.AddColumn(WithFiscal, \"date_key\", each\n"
        f"            Date.Year([date]) * 10000 + Date.Month([date]) * 100 + "
        f"Date.Day([date]), Int64.Type)\n"
        f"    in\n"
        f"        WithKey"
    )


def e_build_dimension(step: TransformationStep, prev: str) -> tuple[str, str]:
    p = step.params
    name = _step_name(step)
    if p.get("from_columns"):
        key = p.get("key", "value")
        sources = []
        for ref in p["from_columns"]:
            table, column = ref.split(".", 1)
            sources.append(f"Table.Column({ident(table)}, {mstr(column)})")
        return name, (
            f"// A conformed dimension built from the distinct values of every table\n"
            f"    // that records this attribute, so one slicer filters all of them.\n"
            f"    let\n"
            f"        AllValues = List.Combine({{{', '.join(sources)}}}),\n"
            f"        Cleaned   = List.Distinct(List.RemoveNulls("
            f"List.Transform(AllValues, each Text.Trim(Text.From(_))))),\n"
            f"        Sorted    = List.Sort(Cleaned, Order.Ascending),\n"
            f"        AsTable   = Table.FromList(Sorted, Splitter.SplitByNothing(), "
            f"{{{mstr(key)}}}),\n"
            f"        Indexed   = Table.AddIndexColumn(AsTable, {mstr(key + '_key')}, 1, 1, "
            f"Int64.Type)\n"
            f"    in\n"
            f"        Indexed"
        )
    if p.get("add_unknown_member"):
        key = p.get("key", "")
        unknown = p.get("unknown_key", "UNASSIGNED")
        label = p.get("unmatched_label", "Not in the dimension")
        sources = [str(r) for r in (p.get("adopt_unmatched_from") or [])]
        if not sources:
            return name, (
                f"// Fact rows whose key is blank would drop out of every sliced\n"
                f"    // total. This explicit member keeps them visible.\n"
                f"    let\n"
                f"        Blank   = Table.FromRecords({{Record.FromList(\n"
                f"            List.Transform(Table.ColumnNames({prev}), each null),\n"
                f"            Table.ColumnNames({prev}))}}),\n"
                f"        Member  = Table.ReplaceValue(Blank, null, {mstr(unknown)},\n"
                f"            Replacer.ReplaceValue, {{{mstr(key)}}}),\n"
                f"        Unioned = Table.Combine({{{prev}, Member}})\n"
                f"    in\n"
                f"        Unioned"
            )
        fact_keys = ", ".join(
            f"Table.Column({ident(r.split('.', 1)[0])}, {mstr(r.split('.', 1)[1])})"
            for r in sources
        )
        return name, (
            f"// A fact key with no row here would drop out of every sliced total, so\n"
            f"    // the report would not reconcile to the source. Each unrecognised\n"
            f"    // code gets its own row, labelled, so it stays visible and\n"
            f"    // identifiable rather than vanishing or being lumped together.\n"
            f"    let\n"
            f"        FactKeys  = List.Distinct(List.RemoveNulls(List.Combine("
            f"{{{fact_keys}}}))),\n"
            f"        Known     = List.Distinct(Table.Column({prev}, {mstr(key)})),\n"
            f"        Missing   = List.Difference(FactKeys, Known),\n"
            f"        AllKeys   = List.Distinct(List.Combine({{Missing, "
            f"{{{mstr(unknown)}}}}})),\n"
            f"        Template  = Table.ColumnNames({prev}),\n"
            f"        NewRows   = Table.FromRecords(List.Transform(AllKeys, (k) =>\n"
            f"            Record.FromList(\n"
            f"                List.Transform(Template, (c) =>\n"
            f"                    if c = {mstr(key)} then k else {mstr(label)}),\n"
            f"                Template))),\n"
            f"        Unioned   = Table.Combine({{{prev}, NewRows}})\n"
            f"    in\n"
            f"        Unioned"
        )
    cols = p.get("columns")
    if cols:
        return name, (
            f"Table.Distinct(Table.SelectColumns({prev}, {mlist(cols)}, "
            f"MissingField.Ignore))"
        )
    return name, f"Table.Distinct({prev})"


EMITTERS: dict[TransformOp, Emitter] = {
    TransformOp.RENAME_COLUMN: e_rename,
    TransformOp.SELECT_COLUMNS: e_select,
    TransformOp.DROP_COLUMN: e_drop,
    TransformOp.CAST_TYPE: e_cast,
    TransformOp.PARSE_DATE: e_parse_date,
    TransformOp.PARSE_NUMBER: e_parse_number,
    TransformOp.PARSE_CURRENCY: e_parse_currency,
    TransformOp.PARSE_PERCENTAGE: e_parse_percentage,
    TransformOp.TRIM_WHITESPACE: e_trim,
    TransformOp.NORMALISE_CASE: e_normalise_case,
    TransformOp.NORMALISE_IDENTIFIER: e_normalise_identifier,
    TransformOp.MAP_CATEGORY: e_map_category,
    TransformOp.FILL_MISSING: e_fill_missing,
    TransformOp.FLAG_MISSING: e_flag_missing,
    TransformOp.REMOVE_EMPTY_ROWS: e_remove_empty_rows,
    TransformOp.DEDUPLICATE: e_deduplicate,
    TransformOp.ADD_SURROGATE_KEY: e_surrogate_key,
    TransformOp.ADD_COMPUTED_COLUMN: e_computed,
    TransformOp.SPLIT_COLUMN: e_split,
    TransformOp.UNPIVOT_COLUMNS: e_unpivot,
    TransformOp.FILTER_ROWS: e_filter,
    TransformOp.APPEND_TABLES: e_append,
    TransformOp.JOIN_TABLES: e_join,
    TransformOp.AGGREGATE: e_aggregate,
    TransformOp.BUILD_DATE_TABLE: e_date_table,
    TransformOp.BUILD_DIMENSION: e_build_dimension,
}


def source_expression(csv_relative_path: str, columns: list[str]) -> str:
    """The M that reads a prepared dataset from the project folder.

    Uses a ``DataFolder`` parameter so a Power BI developer can repoint the
    project at their own copy of the data without editing every query.
    """
    return (
        f"Csv.Document(\n"
        f"            File.Contents(DataFolder & {mstr(csv_relative_path)}),\n"
        f"            [Delimiter = \",\", Columns = {len(columns)}, "
        f"Encoding = 65001, QuoteStyle = QuoteStyle.Csv]\n"
        f"        )"
    )


def build_source_query(
    table_name: str, csv_relative_path: str, columns: list[str],
    steps: list[TransformationStep], *, skipped: list[str] | None = None,
) -> str:
    """M for one **source** table: read its raw extract, then clean it.

    This is the layer that makes the cleaning editable. It reads the raw
    extract shipped in the project, not the already-prepared output, so every
    step below actually does something and a Power BI developer can change it.
    """
    lines: list[str] = [
        f"// Source query for '{table_name}', generated by Agent Analyst.",
        "//",
        "// Reads the RAW extract of this table and applies the approved cleaning",
        "// steps. Each step is here so the cleaning is visible and editable rather",
        "// than baked into a pre-processed file.",
        "let",
        f"    Source = {source_expression(csv_relative_path, columns)},",
        "    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),",
    ]
    previous = "Promoted"
    for step in steps:
        emitter = EMITTERS.get(step.op)
        if emitter is None:
            lines.append(f"    // No Power Query equivalent for '{step.op.value}'.")
            continue
        name, expression = emitter(step, previous)
        lines.append(f"    // {step.title or step.op.value}")
        for chunk in _wrap(step.reason, 74) if step.reason else []:
            lines.append(f"    //   {chunk}")
        lines.append(f"    {ident(name)} = {expression},")
        previous = ident(name)
    for note in skipped or []:
        lines.append(f"    // SKIPPED: {note}")
    if lines[-1].endswith(","):
        lines[-1] = lines[-1][:-1]
    lines += ["in", f"    {previous}"]
    return "\n".join(lines)


def build_dataset_query(
    dataset_name: str, steps: list[TransformationStep],
    *, source_queries: list[str], skipped: list[str] | None = None,
) -> str:
    """M for an **output** dataset built from one or more source queries.

    Unlike the source layer this reads no file: it starts from the already-
    cleaned source queries by name, so the append/dimension/calendar logic is
    applied exactly once.
    """
    lines: list[str] = [
        f"// Dataset query for '{dataset_name}', generated by Agent Analyst.",
        "//",
        "// Built from the cleaned source quer"
        + ("ies: " if len(source_queries) != 1 else "y: ")
        + (", ".join(source_queries) if source_queries else "(none -- generated in full)"),
        "// No file is read here, so the steps below are applied exactly once.",
        "let",
    ]
    previous = ""
    if not steps:
        # A pass-through dataset: one cleaned source, nothing further to do.
        single = source_queries[0] if source_queries else "null"
        lines += ["    Result = " + ident(single) if source_queries else "    Result = null",
                  "in", "    Result"]
        return "\n".join(lines)

    for i, step in enumerate(steps):
        emitter = EMITTERS.get(step.op)
        if emitter is None:
            lines.append(f"    // No Power Query equivalent for '{step.op.value}'.")
            continue
        base = previous or (ident(source_queries[0]) if source_queries else "null")
        name, expression = emitter(step, base)
        lines.append(f"    // {step.title or step.op.value}")
        for chunk in _wrap(step.reason, 74) if step.reason else []:
            lines.append(f"    //   {chunk}")
        lines.append(f"    {ident(name)} = {expression},")
        previous = ident(name)
    for note in skipped or []:
        lines.append(f"    // SKIPPED: {note}")
    if lines[-1].endswith(","):
        lines[-1] = lines[-1][:-1]
    lines += ["in", f"    {previous or 'null'}"]
    return "\n".join(lines)


#: Kept for callers that want the single-layer form (a dataset that is exactly
#: one cleaned source table).
build_query = build_source_query


def _wrap(text: str, width: int) -> list[str]:
    words, out, line = text.split(), [], ""
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out
