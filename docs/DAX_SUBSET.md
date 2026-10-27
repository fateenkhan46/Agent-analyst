# The evaluable DAX subset

There is no Analysis Services engine on Linux, so a generated measure is
checked by three independent gates. `ValidationResult.engine` always records
which one produced the verdict, and **a measure outside the evaluable subset is
never reported as passing** — it is marked `not_supported` with the construct
named.

| Gate | `engine` | What it proves |
|---|---|---|
| 1. Syntax | `dax_parser` | The expression is well-formed DAX (hand-written lexer + recursive-descent parser). |
| 2. References | `dax_reference_check` | Every table, column, measure and function exists in the approved model; argument counts are right; no bare column in a measure; no time intelligence without a date table. |
| 3. Evaluation | `dax_duckdb_transpiled` | The measure was **executed over the real prepared data** and returned a number QA compares against an independently-computed expectation. |

Gate 3 is a tree-walking interpreter whose *leaves* are SQL aggregates. Each
aggregation becomes its own `SELECT` against its own base table; the combining
arithmetic runs in Python. That is deliberate: transpiling
`DIVIDE([Total Budget] - [Total Cost], [Total Budget])` into one SQL statement
over a join would multiply each project's budget by its number of cost rows —
the exact double-counting this system exists to catch. DAX does not work that
way, and neither does this.

## Supported

**Aggregations** (over a qualified column)
`SUM` `AVERAGE` `MIN` `MAX` `COUNT` `COUNTA` `MEDIAN` `DISTINCTCOUNT`
`DISTINCTCOUNTNOBLANK` `STDEV.P` `STDEV.S` `VAR.P` `VAR.S`

**Row counts**
`COUNTROWS(<table>)`, `COUNTROWS(DISTINCT(<col>))`, `COUNTROWS(VALUES(<col>))`,
`COUNTROWS(FILTER(<table>, <simple predicate>))`

**Scalar**
`DIVIDE` `IF` `IFERROR` `SWITCH` `BLANK` `ISBLANK` `COALESCE` `NOT` `ABS`
`ROUND` `INT` `TRUNC` `SQRT` `POWER` `SIGN` `MOD`, two-argument `MIN`/`MAX`,
`SELECTEDVALUE`

**Operators** `+` `-` `*` `/` `^` `&` `=` `<>` `<` `>` `<=` `>=` `&&` `||`
`NOT`, unary `-`, and `IN {literal, ...}`

**Context** `CALCULATE` / `CALCULATETABLE` with `AND`-combined predicates,
`KEEPFILTERS`, `ALL` / `REMOVEFILTERS` (whole table or named columns),
`FILTER(<table>, <simple predicate>)`

**Variables** `VAR` … `RETURN`, including nested scopes

**Measure references** `[Measure Name]`, resolved recursively in the current
filter context (cycles are detected and refused)

**Time intelligence** `TOTALYTD` `TOTALQTD` `TOTALMTD` `DATESYTD` `DATESQTD`
`DATESMTD` `SAMEPERIODLASTYEAR` `PREVIOUSDAY` `PREVIOUSMONTH`
`PREVIOUSQUARTER` `PREVIOUSYEAR` `NEXTMONTH` `NEXTYEAR` `DATEADD`
`PARALLELPERIOD` — each relative to the date bounds currently in context, and
only when the model has a date table

**Relationships** one hop from the aggregated table to a filtered dimension,
in either direction, resolved from the model's active relationships

## Not supported — reported, never guessed

| Construct | Why, and what you get |
|---|---|
| `RANKX`, `TOPN`, `SUMMARIZE`, `ADDCOLUMNS`, `GROUPBY`, `GENERATE` | Table-valued iteration over a computed table. Reported as `unsupported_construct: function`. |
| `SUMX`/`AVERAGEX` over an expression | Row-context iteration is not interpreted. Gates 1 and 2 still apply. |
| `OR` inside a `CALCULATE` filter | Only `AND`-combined predicates become SQL. |
| `ALLEXCEPT` | Its filter semantics are not reproduced. |
| `USERELATIONSHIP` | The result reflects the **active** relationship, and a note says so. |
| `EARLIER`, `RELATED` inside an evaluated expression | Needs a row context the interpreter does not maintain. |
| Multi-hop relationship paths | A filter needing two or more hops is refused rather than approximated. |
| `IN <table expression>` | Only `IN {literals}` is evaluated. |
| `DEFINE` / `EVALUATE` | Query language, not a measure body; refused at parse time. |

## Known divergences from the Power BI engine

These are differences a reviewer should know about, not bugs:

* **Blank arithmetic.** `BLANK() + 1` is `1` in both. But `BLANK() <> FALSE()`
  is `FALSE` in DAX (blank coerces to false) and `TRUE` in SQL. The generator
  therefore never emits a `<> FALSE` filter — flag columns are always written
  non-null by the preparation step so `= TRUE` means the same thing in both.
* **Division.** A bare `/` by zero raises here and returns infinity in DAX;
  `DIVIDE` returns blank in both. The generator always uses `DIVIDE`.
* **Column types.** Prepared data is read as text and cast explicitly
  (`TRY_CAST`), so a value that will not cast becomes blank rather than an
  error. VertiPaq would reject it at load time.
* **No VertiPaq.** Compression, cardinality effects and query-plan behaviour
  are not represented. Nothing here predicts Power BI performance.

A measure that passes all three gates has correct syntax, resolves entirely
against the approved model, and produces an arithmetically verified number over
the real data. It has **not** been run by the Power BI engine, and no part of
this system claims otherwise.
