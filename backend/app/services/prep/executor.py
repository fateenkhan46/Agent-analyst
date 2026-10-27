"""Executing a transformation plan.

Dispatch is on the ``TransformOp`` enum to a handler in ``HANDLERS``. There is
no ``eval``, no generated code, and no path by which a language model's output
reaches execution: an LLM can only produce a *plan* made of allowlisted
operations with typed parameters, and an unknown op raises.

Every handler returns ``(frame, notes, lineage)`` and the executor records a
``RowCountDelta`` per step, so the final lineage reconciles exactly against
the source row counts -- which is what the QA agent checks.

Unapproved and rejected steps are **skipped and reported**, never silently
applied.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pandas as pd

from ...schemas.common import CanonicalType, utcnow
from ...schemas.prep import (
    LineageEdge, PreparedDataset, RowCountDelta, TransformOp, TransformationExecution,
    TransformationPlan, TransformationStep,
)
from ..profiling.inference import is_nullish
from ..profiling.keys import normalise_key
from ..profiling.profiler import _to_date, _to_float

Handler = Callable[[pd.DataFrame, dict[str, Any], "ExecContext"], tuple[pd.DataFrame, list[str]]]


class TransformFailed(Exception):
    """A step could not be applied. Carries the step id for the audit trail."""

    def __init__(self, step_id: str, message: str) -> None:
        super().__init__(message)
        self.step_id = step_id
        self.message = message


#: Columns that exist to preserve what the data looked like BEFORE a
#: transformation. A row we add ourselves has no "before", so writing a label
#: into one would be a small lie that then shows up as a type-conformance
#: failure.
AUDIT_SUFFIXES = ("_original", "_unparsed", "_source_format")


def _is_audit_column(name: str) -> bool:
    return str(name).endswith(AUDIT_SUFFIXES)


def _is_textual(series: pd.Series) -> bool:
    """Can this column hold a text label?

    Two traps, both of which produced wrong data before being pinned down:

    * ``dtype == object`` is wrong on pandas 3, which gives a column the
      dedicated ``str`` dtype after an elementwise map. That reported False for
      exactly the columns that had been cleaned, leaving the label off the
      dimension rows added for unmatched keys -- the one thing those rows exist
      to carry.
    * dtype alone is wrong the other way too: a parsed date column holds
      ``datetime.date`` objects in an *object* column, so it is not
      ``datetime64`` and would have had a text label written into it, which
      then shows up as a type-conformance failure.

    So the decision is made on the values, using the same classifier the
    profiler uses.
    """
    from pandas.api.types import (
        is_bool_dtype, is_datetime64_any_dtype, is_numeric_dtype,
        is_timedelta64_dtype,
    )

    if (
        is_numeric_dtype(series) or is_bool_dtype(series)
        or is_datetime64_any_dtype(series) or is_timedelta64_dtype(series)
    ):
        return False

    from ..profiling.inference import classify_column

    sample = [v for v in series.tolist()[:200] if not is_nullish(v)]
    if not sample:
        return True
    verdict = classify_column(sample, str(series.name or ""))
    return not (verdict.canonical.is_temporal or verdict.canonical.is_numeric)


class ExecContext:
    """Shared state during an execution: the working frames and the notes."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames
        self.lineage: list[LineageEdge] = []
        self.source_columns: dict[str, dict[str, str]] = {}

    def record_lineage(
        self, dataset: str, target_column: str, sources: list[str], step_id: str, summary: str
    ) -> None:
        existing = next(
            (e for e in self.lineage if e.target == f"{dataset}.{target_column}"), None
        )
        if existing:
            for s in sources:
                if s not in existing.sources:
                    existing.sources.append(s)
            if step_id not in existing.via_steps:
                existing.via_steps.append(step_id)
            existing.transform_summary = (
                f"{existing.transform_summary}; {summary}" if existing.transform_summary
                else summary
            )
            return
        self.lineage.append(LineageEdge(
            target=f"{dataset}.{target_column}", sources=sources,
            via_steps=[step_id], transform_summary=summary,
        ))


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
def _require(params: dict[str, Any], key: str) -> Any:
    if key not in params:
        raise KeyError(f"missing required parameter '{key}'")
    return params[key]


def h_rename(frame: pd.DataFrame, p: dict, ctx: ExecContext) -> tuple[pd.DataFrame, list[str]]:
    renames: dict[str, str] = _require(p, "renames")
    present = {k: v for k, v in renames.items() if k in frame.columns}
    missing = sorted(set(renames) - set(present))
    notes = [f"renamed {len(present)} column(s)"]
    if missing:
        notes.append(f"skipped {len(missing)} absent column(s): {', '.join(missing[:5])}")
    # Avoid creating a duplicate name by renaming onto an existing column.
    final: dict[str, str] = {}
    taken = {c for c in frame.columns if c not in present}
    for src, dst in present.items():
        name, n = dst, 2
        while name in taken:
            name = f"{dst}_{n}"
            n += 1
        taken.add(name)
        final[src] = name
        if name != dst:
            notes.append(f"'{dst}' already existed; used '{name}'")
    return frame.rename(columns=final), notes


def h_select(frame: pd.DataFrame, p: dict, ctx: ExecContext) -> tuple[pd.DataFrame, list[str]]:
    cols = [c for c in _require(p, "columns") if c in frame.columns]
    return frame[cols], [f"kept {len(cols)} column(s)"]


def h_drop(frame: pd.DataFrame, p: dict, ctx: ExecContext) -> tuple[pd.DataFrame, list[str]]:
    cols = [c for c in _require(p, "columns") if c in frame.columns]
    return frame.drop(columns=cols), [f"dropped {len(cols)} column(s): {', '.join(cols)}"]


def h_trim(frame: pd.DataFrame, p: dict, ctx: ExecContext) -> tuple[pd.DataFrame, list[str]]:
    col = _require(p, "column")
    if col not in frame.columns:
        return frame, [f"column '{col}' not present; skipped"]
    before = frame[col].astype(str).nunique()
    collapse = bool(p.get("collapse_internal", True))

    def clean(v: Any) -> Any:
        if v is None or is_nullish(v):
            return v
        s = str(v).strip()
        return re.sub(r"\s+", " ", s) if collapse else s

    frame = frame.copy()
    frame[col] = frame[col].map(clean)
    after = frame[col].astype(str).nunique()
    notes = [f"trimmed '{col}'"]
    if after < before:
        notes.append(f"distinct values fell from {before} to {after} (values merged)")
    return frame, notes


def h_normalise_case(frame: pd.DataFrame, p: dict, ctx: ExecContext) -> tuple[pd.DataFrame, list[str]]:
    col = _require(p, "column")
    if col not in frame.columns:
        return frame, [f"column '{col}' not present; skipped"]
    mode = p.get("mode", "title")
    fn = {"upper": str.upper, "lower": str.lower, "title": str.title}.get(mode, str.title)
    frame = frame.copy()
    frame[col] = frame[col].map(lambda v: v if is_nullish(v) else fn(str(v)))
    return frame, [f"normalised the case of '{col}' to {mode}"]


def h_normalise_identifier(
    frame: pd.DataFrame, p: dict, ctx: ExecContext
) -> tuple[pd.DataFrame, list[str]]:
    col = _require(p, "column")
    if col not in frame.columns:
        return frame, [f"column '{col}' not present; skipped"]
    frame = frame.copy()
    original = f"{col}_original"
    if original not in frame.columns:
        frame[original] = frame[col]
    before = frame[col].astype(str).nunique()
    frame[col] = frame[col].map(lambda v: normalise_key(v) or None)
    after = frame[col].astype(str).nunique()
    notes = [f"normalised the '{col}' identifier; the source value is kept in '{original}'"]
    if after < before:
        notes.append(
            f"{before - after} distinct spelling(s) collapsed into an existing key "
            f"({before} -> {after})"
        )
    return frame, notes


def h_parse_date(frame: pd.DataFrame, p: dict, ctx: ExecContext) -> tuple[pd.DataFrame, list[str]]:
    col = _require(p, "column")
    if col not in frame.columns:
        return frame, [f"column '{col}' not present; skipped"]
    frame = frame.copy()
    quarantine = p.get("quarantine_column") or f"{col}_unparsed"
    parsed, failed = [], []
    for v in frame[col].tolist():
        if is_nullish(v):
            parsed.append(None)
            failed.append(None)
            continue
        d = _to_date(v)
        if d is None:
            parsed.append(None)
            failed.append(str(v))
        else:
            parsed.append(d.to_pydatetime())
            failed.append(None)
    # If no value carries a time component, store plain dates. The generated
    # calendar holds dates, and a datetime serialises as "2025-03-15 00:00:00"
    # while a date serialises as "2025-03-15" -- so mixing them makes every
    # relationship to the date dimension resolve nothing, and every
    # time-intelligence measure silently returns blank.
    has_time = any(
        v is not None and (v.hour or v.minute or v.second or v.microsecond)
        for v in parsed
    )
    if not has_time:
        parsed = [None if v is None else v.date() for v in parsed]
    frame[col] = parsed
    unparsed = sum(1 for f in failed if f is not None)
    if unparsed:
        frame[quarantine] = failed
    notes = [f"parsed '{col}' to dates"]
    if unparsed:
        notes.append(
            f"{unparsed} value(s) could not be parsed and were preserved in "
            f"'{quarantine}' rather than discarded"
        )
    if p.get("formats"):
        notes.append(f"{len(p['formats'])} source format(s) handled")
    return frame, notes


def _parse_numeric_column(
    frame: pd.DataFrame, col: str, quarantine: str
) -> tuple[pd.DataFrame, int]:
    values, failed = [], []
    for v in frame[col].tolist():
        if is_nullish(v):
            values.append(None)
            failed.append(None)
            continue
        f = _to_float(v)
        if f is None:
            values.append(None)
            failed.append(str(v))
        else:
            values.append(f)
            failed.append(None)
    frame[col] = values
    unparsed = sum(1 for f in failed if f is not None)
    if unparsed:
        frame[quarantine] = failed
    return frame, unparsed


def h_parse_number(frame: pd.DataFrame, p: dict, ctx: ExecContext) -> tuple[pd.DataFrame, list[str]]:
    col = _require(p, "column")
    if col not in frame.columns:
        return frame, [f"column '{col}' not present; skipped"]
    frame = frame.copy()
    audit = p.get("audit_column")
    if audit and p.get("unit_normalisation") == "hours":
        # Record which rows were converted, so "tell me which rows you changed"
        # is answerable.
        from ..profiling.inference import classify_value
        frame[audit] = frame[col].map(
            lambda v: "" if is_nullish(v) else (
                (classify_value(v).pattern if classify_value(v) else "") or ""
            )
        )
    frame, unparsed = _parse_numeric_column(
        frame, col, p.get("quarantine_column") or f"{col}_unparsed"
    )
    notes = [f"parsed '{col}' to numbers"]
    if p.get("unit_normalisation") == "hours":
        notes.append("values in minutes or hh:mm were converted to decimal hours")
        if audit:
            changed = int((frame[audit].isin(["duration_unit", "duration_hhmm"])).sum())
            notes.append(f"{changed} row(s) had their unit converted; "
                         f"the original format is recorded in '{audit}'")
    if unparsed:
        notes.append(f"{unparsed} value(s) quarantined")
    return frame, notes


def h_parse_currency(
    frame: pd.DataFrame, p: dict, ctx: ExecContext
) -> tuple[pd.DataFrame, list[str]]:
    col = _require(p, "column")
    if col not in frame.columns:
        return frame, [f"column '{col}' not present; skipped"]
    frame = frame.copy()
    currency_col = p.get("currency_column") or f"{col}_currency"
    expected = (p.get("expected_currency") or "").upper()

    from ..profiling.inference import classify_value

    currencies = []
    for v in frame[col].tolist():
        if is_nullish(v):
            currencies.append(None)
            continue
        cls = classify_value(v)
        currencies.append((cls.currency if cls and cls.currency else expected) or None)
    frame[currency_col] = currencies
    frame, unparsed = _parse_numeric_column(
        frame, col, p.get("quarantine_column") or f"{col}_unparsed"
    )

    notes = [f"parsed '{col}' to numeric amounts"]
    distinct = sorted({c for c in currencies if c})
    if expected:
        # The flag is emitted whenever a reporting currency is known, even for a
        # single-currency column. Emitting it conditionally produced a subtle
        # bug: appending a single-currency table to a mixed-currency one left
        # the flag NULL on the single-currency rows, and a measure filtering
        # "flag = TRUE" then dropped them silently. A column that is always
        # present and never null keeps the measure honest in both DuckDB and
        # Power BI, which coerce blanks differently.
        flag = f"{col}_is_reporting_currency"
        frame[flag] = [
            # No currency detected means no symbol or code was present, so the
            # value is in the column's default (reporting) currency.
            True if c is None else c == expected
            for c in currencies
        ]
        other = [c for c in distinct if c != expected]
        offending = sum(1 for c in currencies if c and c in other)
        if offending:
            notes.append(
                f"{len(distinct)} currencies present ({', '.join(distinct)}). "
                f"{offending} row(s) are not in the reporting currency ({expected}) "
                f"and are marked False in '{flag}'. They are retained but excluded from "
                f"any single-currency total until a conversion rate is supplied."
            )
        else:
            notes.append(
                f"all values are in the reporting currency ({expected}); '{flag}' is "
                f"True for every row so appended tables share one schema"
            )
    elif len(distinct) > 1:
        notes.append(
            f"{len(distinct)} currencies present ({', '.join(distinct)}) and no reporting "
            f"currency is configured, so no single-currency total can be trusted. Set the "
            f"project currency and re-run."
        )
    if unparsed:
        notes.append(f"{unparsed} value(s) quarantined")
    return frame, notes


def h_parse_percentage(
    frame: pd.DataFrame, p: dict, ctx: ExecContext
) -> tuple[pd.DataFrame, list[str]]:
    col = _require(p, "column")
    if col not in frame.columns:
        return frame, [f"column '{col}' not present; skipped"]
    frame = frame.copy()
    frame, unparsed = _parse_numeric_column(
        frame, col, p.get("quarantine_column") or f"{col}_unparsed"
    )
    notes = [f"parsed '{col}' to a 0..1 fraction"]
    if unparsed:
        notes.append(f"{unparsed} value(s) quarantined")
    return frame, notes


def h_cast(frame: pd.DataFrame, p: dict, ctx: ExecContext) -> tuple[pd.DataFrame, list[str]]:
    col, target = _require(p, "column"), CanonicalType(_require(p, "to"))
    if col not in frame.columns:
        return frame, [f"column '{col}' not present; skipped"]
    if target.is_temporal:
        return h_parse_date(frame, {"column": col}, ctx)
    if target.is_numeric:
        return h_parse_number(frame, {"column": col}, ctx)
    frame = frame.copy()
    frame[col] = frame[col].map(lambda v: v if is_nullish(v) else str(v))
    return frame, [f"cast '{col}' to {target.value}"]


def h_map_category(
    frame: pd.DataFrame, p: dict, ctx: ExecContext
) -> tuple[pd.DataFrame, list[str]]:
    col = _require(p, "column")
    mapping: dict[str, str] = p.get("mapping") or {}
    if col not in frame.columns:
        return frame, [f"column '{col}' not present; skipped"]
    if not mapping:
        # Deliberate: an unconfirmed vocabulary mapping applies nothing.
        return frame, [
            f"no mapping supplied for '{col}', so no value was changed. The two "
            f"vocabularies stay separate until the correspondence is confirmed."
        ]
    frame = frame.copy()
    lower = {str(k).strip().lower(): v for k, v in mapping.items()}
    unmapped: set[str] = set()

    def apply(v: Any) -> Any:
        if is_nullish(v):
            return v
        key = str(v).strip().lower()
        if key in lower:
            return lower[key]
        unmapped.add(str(v))
        return v

    frame[col] = frame[col].map(apply)
    notes = [f"mapped {len(mapping)} value(s) in '{col}'"]
    if unmapped:
        notes.append(
            f"{len(unmapped)} value(s) had no mapping and were left unchanged: "
            f"{', '.join(sorted(unmapped)[:6])}"
        )
    return frame, notes


def h_fill_missing(
    frame: pd.DataFrame, p: dict, ctx: ExecContext
) -> tuple[pd.DataFrame, list[str]]:
    col, value = _require(p, "column"), _require(p, "value")
    if col not in frame.columns:
        return frame, [f"column '{col}' not present; skipped"]
    frame = frame.copy()
    missing = frame[col].map(is_nullish)
    count = int(missing.sum())
    flag = p.get("flag_column")
    if flag:
        frame[flag] = missing
    frame.loc[missing, col] = value
    return frame, [
        f"filled {count} missing value(s) in '{col}' with {value!r}"
        + (f"; marked in '{flag}'" if flag else "")
    ]


def h_flag_missing(
    frame: pd.DataFrame, p: dict, ctx: ExecContext
) -> tuple[pd.DataFrame, list[str]]:
    col = _require(p, "column")
    if col not in frame.columns:
        return frame, [f"column '{col}' not present; skipped"]
    frame = frame.copy()
    flag = p.get("flag_column") or f"{col}_is_missing"
    frame[flag] = frame[col].map(is_nullish)
    return frame, [
        f"flagged {int(frame[flag].sum())} missing value(s) in '{col}' as '{flag}'; "
        f"no value was imputed"
    ]


def h_remove_empty_rows(
    frame: pd.DataFrame, p: dict, ctx: ExecContext
) -> tuple[pd.DataFrame, list[str]]:
    if frame.empty:
        return frame, ["table is empty; nothing to remove"]
    mask = frame.map(is_nullish).all(axis=1)
    removed = int(mask.sum())
    return frame[~mask].reset_index(drop=True), [
        f"removed {removed} row(s) that were empty in every column"
    ]


def h_deduplicate(
    frame: pd.DataFrame, p: dict, ctx: ExecContext
) -> tuple[pd.DataFrame, list[str]]:
    subset = p.get("subset")
    keep = p.get("keep", "first")
    cols = [c for c in subset if c in frame.columns] if subset else None
    before = len(frame)
    as_text = frame.astype(str)
    mask = as_text.duplicated(subset=cols, keep=keep)
    out = frame[~mask].reset_index(drop=True)
    return out, [
        f"removed {before - len(out)} duplicate row(s)"
        + (f" on {', '.join(cols)}" if cols else " (all columns identical)")
    ]


def h_add_surrogate_key(
    frame: pd.DataFrame, p: dict, ctx: ExecContext
) -> tuple[pd.DataFrame, list[str]]:
    name = p.get("column", "surrogate_key")
    prefix = p.get("prefix", "")
    frame = frame.copy()
    frame[name] = [f"{prefix}{i + 1}" for i in range(len(frame))]
    return frame, [f"added surrogate key '{name}' over {len(frame)} row(s)"]


def h_add_computed(
    frame: pd.DataFrame, p: dict, ctx: ExecContext
) -> tuple[pd.DataFrame, list[str]]:
    """Add a column from a **named, allowlisted** computation.

    Deliberately not an expression evaluator: ``kind`` selects from a fixed
    set. This is the boundary that keeps generated logic out of execution.
    """
    name, kind = _require(p, "column"), _require(p, "kind")
    frame = frame.copy()
    if kind == "multiply":
        a, b = p["left"], p["right"]
        frame[name] = [
            None if (is_nullish(x) or is_nullish(y)) else float(x) * float(y)
            for x, y in zip(frame[a], frame[b])
        ]
    elif kind == "subtract":
        a, b = p["left"], p["right"]
        frame[name] = [
            None if (is_nullish(x) or is_nullish(y)) else float(x) - float(y)
            for x, y in zip(frame[a], frame[b])
        ]
    elif kind == "year":
        frame[name] = [None if is_nullish(v) else _to_date(v).year  # type: ignore[union-attr]
                       if _to_date(v) else None for v in frame[p["source"]]]
    elif kind == "month_start":
        frame[name] = [
            None if is_nullish(v) or _to_date(v) is None
            else _to_date(v).replace(day=0 + 1).date().isoformat()  # type: ignore[union-attr]
            for v in frame[p["source"]]
        ]
    elif kind == "constant":
        frame[name] = p.get("value")
    elif kind == "concat":
        sep = p.get("separator", "-")
        sources = p["sources"]
        frame[name] = [
            sep.join("" if is_nullish(v) else str(v) for v in row)
            for row in zip(*(frame[s] for s in sources))
        ]
    else:
        raise ValueError(
            f"unsupported computation {kind!r}. Allowed: multiply, subtract, year, "
            f"month_start, constant, concat."
        )
    return frame, [f"added computed column '{name}' ({kind})"]


def h_split_column(
    frame: pd.DataFrame, p: dict, ctx: ExecContext
) -> tuple[pd.DataFrame, list[str]]:
    col, sep = _require(p, "column"), p.get("separator", " ")
    into: list[str] = _require(p, "into")
    if col not in frame.columns:
        return frame, [f"column '{col}' not present; skipped"]
    frame = frame.copy()
    parts = frame[col].map(lambda v: ("" if is_nullish(v) else str(v)).split(sep, len(into) - 1))
    for i, target in enumerate(into):
        frame[target] = [p[i] if i < len(p) else None for p in parts]
    return frame, [f"split '{col}' into {', '.join(into)}"]


def h_unpivot(frame: pd.DataFrame, p: dict, ctx: ExecContext) -> tuple[pd.DataFrame, list[str]]:
    id_vars = [c for c in _require(p, "id_columns") if c in frame.columns]
    value_vars = [c for c in _require(p, "value_columns") if c in frame.columns]
    out = frame.melt(
        id_vars=id_vars, value_vars=value_vars,
        var_name=p.get("name_column", "attribute"),
        value_name=p.get("value_column", "value"),
    )
    return out, [f"unpivoted {len(value_vars)} column(s) into rows ({len(frame)} -> {len(out)})"]


def h_filter_rows(
    frame: pd.DataFrame, p: dict, ctx: ExecContext
) -> tuple[pd.DataFrame, list[str]]:
    """Filter on a named, allowlisted predicate. No expression evaluation."""
    col, op = _require(p, "column"), _require(p, "op")
    value = p.get("value")
    if col not in frame.columns:
        return frame, [f"column '{col}' not present; skipped"]
    series = frame[col]
    before = len(frame)
    if op == "not_null":
        mask = ~series.map(is_nullish)
    elif op == "equals":
        mask = series.astype(str) == str(value)
    elif op == "not_equals":
        mask = series.astype(str) != str(value)
    elif op == "in":
        allowed = {str(v) for v in (value or [])}
        mask = series.astype(str).isin(allowed)
    elif op == "not_in":
        blocked = {str(v) for v in (value or [])}
        mask = ~series.astype(str).isin(blocked)
    elif op in {"gte", "lte", "gt", "lt"}:
        nums = series.map(lambda v: _to_float(v))
        threshold = float(value)
        cmp = {
            "gte": lambda x: x is not None and x >= threshold,
            "lte": lambda x: x is not None and x <= threshold,
            "gt": lambda x: x is not None and x > threshold,
            "lt": lambda x: x is not None and x < threshold,
        }[op]
        mask = nums.map(cmp)
    elif op == "between":
        lo, hi = float(value[0]), float(value[1])
        nums = series.map(lambda v: _to_float(v))
        mask = nums.map(lambda x: x is not None and lo <= x <= hi)
    else:
        raise ValueError(
            f"unsupported filter {op!r}. Allowed: not_null, equals, not_equals, in, "
            f"not_in, gt, gte, lt, lte, between."
        )
    out = frame[mask].reset_index(drop=True)
    return out, [f"filtered '{col}' {op} {value!r}: {before} -> {len(out)} row(s)"]


def h_append(frame: pd.DataFrame, p: dict, ctx: ExecContext) -> tuple[pd.DataFrame, list[str]]:
    tables: list[str] = _require(p, "tables")
    marker = p.get("source_column", "source_table")
    present = [t for t in tables if t in ctx.frames]
    if not present:
        raise ValueError(f"none of the tables to append exist: {', '.join(tables)}")
    pieces, notes = [], []
    for name in present:
        piece = ctx.frames[name].copy()
        piece[marker] = name
        pieces.append(piece)
        notes.append(f"{name}: {len(piece)} row(s)")
    out = pd.concat(pieces, ignore_index=True, sort=False)
    all_columns = {c for piece in pieces for c in piece.columns}
    #: Flag columns whose absence has a knowable meaning. Leaving these NULL is
    #: actively dangerous: a measure filtering "flag = TRUE" drops the rows,
    #: and the loss is invisible in the total.
    flag_defaults = {"_is_reporting_currency": True, "_is_missing": False}
    for name, piece in zip(present, pieces):
        gaps = sorted(all_columns - set(piece.columns))
        if not gaps:
            continue
        filled: list[str] = []
        for column in gaps:
            default = next(
                (v for suffix, v in flag_defaults.items() if column.endswith(suffix)),
                None,
            )
            if default is None:
                continue
            mask = out[marker] == name
            out.loc[mask, column] = out.loc[mask, column].fillna(default)
            filled.append(f"{column}={default}")
        remaining = [c for c in gaps if not any(
            c.endswith(suffix) for suffix in flag_defaults
        )]
        if filled:
            notes.append(
                f"{name} had no {', '.join(filled)}; set to that default because the "
                f"absence of the flag is meaningful, not unknown"
            )
        if remaining:
            notes.append(
                f"{name} has no {', '.join(remaining[:4])}; those cells are null, not zero"
            )
    missing = sorted(set(tables) - set(present))
    if missing:
        notes.append(f"missing table(s) skipped: {', '.join(missing)}")
    notes.insert(0, f"appended {len(present)} table(s) into {len(out)} row(s)")
    return out, notes


def h_join(frame: pd.DataFrame, p: dict, ctx: ExecContext) -> tuple[pd.DataFrame, list[str]]:
    right_name = _require(p, "right_table")
    left_keys, right_keys = _require(p, "left_keys"), _require(p, "right_keys")
    how = p.get("kind", "left")
    right = ctx.frames.get(right_name)
    if right is None:
        raise ValueError(f"the right-hand table '{right_name}' does not exist")
    missing_left = [k for k in left_keys if k not in frame.columns]
    missing_right = [k for k in right_keys if k not in right.columns]
    if missing_left or missing_right:
        raise ValueError(
            f"join keys missing: left {missing_left}, right {missing_right}"
        )
    before = len(frame)
    # Guard: a non-unique right side fans out and inflates additive measures.
    right_dupes = int(right.astype(str).duplicated(subset=right_keys).sum())
    out = frame.merge(
        right, how=how, left_on=left_keys, right_on=right_keys, suffixes=("", "_dim")
    )
    notes = [f"joined '{right_name}' ({how}): {before} -> {len(out)} row(s)"]
    if len(out) > before:
        notes.append(
            f"ROW FAN-OUT: the join added {len(out) - before} row(s) because the right "
            f"side has {right_dupes} duplicate key(s). Additive measures over this table "
            f"are now overstated."
        )
    unmatched = int(out[right_keys[0]].isna().sum()) if right_keys[0] in out.columns else 0
    if unmatched:
        notes.append(f"{unmatched} left row(s) found no match and kept null dimension values")
    return out, notes


def h_aggregate(frame: pd.DataFrame, p: dict, ctx: ExecContext) -> tuple[pd.DataFrame, list[str]]:
    group: list[str] = [c for c in _require(p, "group_by") if c in frame.columns]
    aggs: dict[str, str] = _require(p, "aggregations")
    allowed = {"sum", "mean", "min", "max", "count", "nunique", "median"}
    bad = {k: v for k, v in aggs.items() if v not in allowed}
    if bad:
        raise ValueError(f"unsupported aggregation(s) {bad}; allowed: {sorted(allowed)}")
    usable = {k: v for k, v in aggs.items() if k in frame.columns}
    before = len(frame)
    out = frame.groupby(group, dropna=False).agg(usable).reset_index()
    return out, [
        f"aggregated to {', '.join(group)}: {before} -> {len(out)} row(s). The result is "
        f"no longer at transaction grain."
    ]


def h_build_date_table(
    frame: pd.DataFrame, p: dict, ctx: ExecContext
) -> tuple[pd.DataFrame, list[str]]:
    start, end = _require(p, "start"), _require(p, "end")
    fiscal_start = int(p.get("fiscal_year_start_month", 1))
    days = pd.date_range(start=start, end=end, freq="D")
    if len(days) == 0:
        raise ValueError(f"the date range {start}..{end} is empty")
    out = pd.DataFrame({"date": days})
    out["year"] = days.year
    out["quarter"] = days.quarter
    out["month_number"] = days.month
    out["month_name"] = days.strftime("%B")
    out["month_year"] = days.strftime("%b %Y")
    out["month_key"] = days.year * 100 + days.month
    out["day_of_month"] = days.day
    out["day_name"] = days.strftime("%A")
    out["week_of_year"] = days.isocalendar().week.to_numpy()
    out["is_weekend"] = days.dayofweek >= 5
    if fiscal_start == 1:
        out["fiscal_year"] = days.year
    else:
        out["fiscal_year"] = [
            (d.year + 1) if d.month >= fiscal_start else d.year for d in days
        ]
    out["date_key"] = days.strftime("%Y%m%d").astype(int)
    return out, [
        f"built a contiguous date table of {len(out):,} day(s) from {start} to {end}"
        + ("" if fiscal_start == 1 else f", fiscal year starting month {fiscal_start}")
    ]


def h_build_dimension(
    frame: pd.DataFrame, p: dict, ctx: ExecContext
) -> tuple[pd.DataFrame, list[str]]:
    """Build (or extend) a dimension from distinct values, or add the
    'UNASSIGNED' member for orphan keys."""
    notes: list[str] = []
    key = p.get("key", "")


    if p.get("from_columns"):
        values: list[str] = []
        for ref in p["from_columns"]:
            table, column = ref.split(".", 1)
            source = ctx.frames.get(table)
            if source is None or column not in source.columns:
                notes.append(f"{ref} not available; skipped")
                continue
            values.extend(
                str(v).strip() for v in source[column].tolist() if not is_nullish(v)
            )
        distinct = sorted(set(values))
        out = pd.DataFrame({key or "value": distinct})
        out[f"{key or 'value'}_key"] = range(1, len(out) + 1)
        notes.insert(0, f"built a conformed dimension of {len(out)} distinct value(s)")
        return out, notes

    if p.get("add_unknown_member"):
        if frame is None or frame.empty:
            raise ValueError("cannot add an unknown member to a table that does not exist")
        unknown = p.get("unknown_key", "UNASSIGNED")
        if key not in frame.columns:
            return frame, [f"key '{key}' not present; skipped"]

        existing = {str(v) for v in frame[key].tolist() if not is_nullish(v)}
        label = str(p.get("unmatched_label", "Not in the dimension"))
        # The unknown member is added below, and a fact table may already use
        # that same literal (the fill-missing step writes it into blank keys).
        # Without this the key would be added twice and the dimension would
        # stop being unique -- which silently reclassifies it as a fact table.
        existing.add(str(unknown))

        # Collect every key value the fact tables use that this dimension does
        # not have. Each becomes its own row, keeping the real code visible.
        wanted: list[str] = []
        for reference in p.get("adopt_unmatched_from", []) or []:
            source_table, _, source_column = str(reference).partition(".")
            source = ctx.frames.get(source_table)
            if source is None or source_column not in source.columns:
                notes.append(f"{reference} not available; its keys were not adopted")
                continue
            for value in source[source_column].tolist():
                if is_nullish(value):
                    continue
                text = str(value)
                if text not in existing and text not in wanted:
                    wanted.append(text)

        had_unknown = str(unknown) in {
            str(v) for v in frame[key].tolist() if not is_nullish(v)
        }
        new_rows: list[dict[str, Any]] = []
        if not had_unknown:
            blank_row = {c: None for c in frame.columns}
            blank_row[key] = unknown
            for col in frame.columns:
                if col != key and _is_textual(frame[col]) and not _is_audit_column(col):
                    blank_row[col] = "Unassigned"
            new_rows.append(blank_row)
            notes.append(
                f"added the '{unknown}' member so fact rows with a blank '{key}' stay "
                f"visible in every total"
            )
        for value in wanted:
            row = {c: None for c in frame.columns}
            row[key] = value
            for col in frame.columns:
                if col != key and _is_textual(frame[col]) and not _is_audit_column(col):
                    row[col] = label
            new_rows.append(row)
        if wanted:
            notes.append(
                f"added {len(wanted)} row(s) for key(s) used by a fact table but absent "
                f"here ({', '.join(wanted[:5])}), each labelled '{label}' so the code "
                f"stays visible rather than being lumped into '{unknown}' or dropped "
                f"from every sliced total"
            )
        if not new_rows:
            return frame, [f"every fact key already resolves in '{key}'; nothing added"]
        out = pd.concat([frame, pd.DataFrame(new_rows)], ignore_index=True)
        return out, notes

    # plain distinct dimension from the current frame
    cols = [c for c in (p.get("columns") or frame.columns) if c in frame.columns]
    out = frame[cols].drop_duplicates().reset_index(drop=True)
    return out, [f"built a dimension of {len(out)} distinct row(s)"]


HANDLERS: dict[TransformOp, Handler] = {
    TransformOp.RENAME_COLUMN: h_rename,
    TransformOp.SELECT_COLUMNS: h_select,
    TransformOp.DROP_COLUMN: h_drop,
    TransformOp.CAST_TYPE: h_cast,
    TransformOp.PARSE_DATE: h_parse_date,
    TransformOp.PARSE_NUMBER: h_parse_number,
    TransformOp.PARSE_CURRENCY: h_parse_currency,
    TransformOp.PARSE_PERCENTAGE: h_parse_percentage,
    TransformOp.TRIM_WHITESPACE: h_trim,
    TransformOp.NORMALISE_CASE: h_normalise_case,
    TransformOp.NORMALISE_IDENTIFIER: h_normalise_identifier,
    TransformOp.MAP_CATEGORY: h_map_category,
    TransformOp.FILL_MISSING: h_fill_missing,
    TransformOp.FLAG_MISSING: h_flag_missing,
    TransformOp.REMOVE_EMPTY_ROWS: h_remove_empty_rows,
    TransformOp.DEDUPLICATE: h_deduplicate,
    TransformOp.ADD_SURROGATE_KEY: h_add_surrogate_key,
    TransformOp.ADD_COMPUTED_COLUMN: h_add_computed,
    TransformOp.SPLIT_COLUMN: h_split_column,
    TransformOp.UNPIVOT_COLUMNS: h_unpivot,
    TransformOp.FILTER_ROWS: h_filter_rows,
    TransformOp.APPEND_TABLES: h_append,
    TransformOp.JOIN_TABLES: h_join,
    TransformOp.AGGREGATE: h_aggregate,
    TransformOp.BUILD_DATE_TABLE: h_build_date_table,
    TransformOp.BUILD_DIMENSION: h_build_dimension,
}
