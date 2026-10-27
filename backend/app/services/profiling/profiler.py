"""Building ``ColumnProfile`` / ``SourceTable`` from a DataFrame.

Every number here is measured. The only judgement calls are the thresholds
below, which are named constants so they can be reviewed and tuned rather
than being magic numbers buried in branches.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter
from typing import Any

import pandas as pd

from ...schemas.common import CanonicalType, Confidence, Issue, Severity
from ...schemas.source import (
    ColumnProfile,
    FormatVariant,
    NumericStats,
    Outlier,
    SourceTable,
    TemporalStats,
)
from .inference import (
    ID_NAME_RE,
    classify_column,
    classify_value,
    is_nullish,
    naming_convention,
)

# --- review thresholds ---------------------------------------------------
HIGH_NULL_SHARE = 0.30
CRITICAL_NULL_SHARE = 0.80
PK_MIN_SCORE = 0.85
LOW_TYPE_CONFORMANCE = 0.90
OUTLIER_Z = 4.0
MAX_OUTLIERS = 25
HIGH_CARDINALITY_TEXT = 0.95
#: At/above this share of distinct values, a column is "almost a key".
NEAR_UNIQUE_RATIO = 0.95
#: ...or it repeats at most this many values in absolute terms. A share alone
#: misses small tables, where a single duplicate is a large fraction of rows.
MAX_ABSOLUTE_KEY_DUPES = 5
#: Floor below which a column is a genuine category, not a damaged key.
MIN_KEY_CARDINALITY = 0.60
MAX_COMPOSITE_KEY_WIDTH = 3


def _as_text(v: Any) -> str:
    return "" if v is None else str(v)


def _non_null(series: pd.Series) -> list[Any]:
    return [v for v in series.tolist() if not is_nullish(v)]


def _to_float(raw: Any) -> float | None:
    """Parse a business-formatted number into a float, or None.

    Handles: currency symbols, comma/dot thousands, European decimal commas,
    accounting parentheses, trailing minus, percentages, unit suffixes.
    Deliberately conservative -- ambiguity returns None rather than a guess.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return None if (isinstance(raw, float) and math.isnan(raw)) else float(raw)
    s = str(raw).strip()
    if not s or is_nullish(s):
        return None

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    if s.endswith("-"):
        negative, s = True, s[:-1].strip()

    percent = s.endswith("%")
    if percent:
        s = s[:-1].strip()

    divisor = 1.0
    low = s.lower()
    for suffix, div in (("hrs", 1), ("hr", 1), ("hours", 1), ("hour", 1), ("h", 1),
                        ("mins", 60), ("min", 60), ("minutes", 60), ("minute", 60), ("m", 60),
                        ("days", 1 / 24), ("day", 1 / 24), ("d", 1 / 24)):
        if low.endswith(suffix) and low[: -len(suffix)].strip().replace(".", "", 1).replace(",", "").isdigit():
            s = s[: -len(suffix)].strip()
            divisor = float(div)
            break

    if ":" in s:  # hh:mm[:ss] duration
        parts = s.split(":")
        if all(p.strip().isdigit() for p in parts) and 2 <= len(parts) <= 3:
            h = float(parts[0]); mi = float(parts[1]); se = float(parts[2]) if len(parts) == 3 else 0.0
            val = h + mi / 60 + se / 3600
            return -val if negative else val

    import re as _re
    s = _re.sub(r"[A-Z]{3}|[$£€¥₹₩₽]|R\$|A\$|C\$", "", s).strip()
    s = s.replace(" ", " ").replace(" ", " ")

    has_comma, has_dot = "," in s, "." in s
    if has_comma and has_dot:
        # whichever appears last is the decimal separator
        s = s.replace(",", "") if s.rindex(".") > s.rindex(",") else s.replace(".", "").replace(",", ".")
    elif has_comma:
        frag = s.split(",")
        if len(frag) == 2 and len(frag[1]) in (1, 2):
            s = s.replace(",", ".")          # 1234,5 -> decimal comma
        elif all(len(f) == 3 for f in frag[1:]):
            s = s.replace(",", "")           # 1,234,567 -> thousands
        else:
            return None
    elif has_dot:
        frag = s.split(".")
        if len(frag) > 2 and all(len(f) == 3 for f in frag[1:]):
            s = s.replace(".", "")           # 1.234.567 -> thousands
    s = s.replace(" ", "")
    try:
        val = float(s)
    except ValueError:
        return None
    val = val / divisor
    if percent:
        val = val / 100.0
    return -val if negative else val


def _to_date(raw: Any) -> pd.Timestamp | None:
    if raw is None or is_nullish(raw):
        return None
    if isinstance(raw, pd.Timestamp):
        return raw
    try:
        import datetime as _dt
        if isinstance(raw, _dt.datetime):
            return pd.Timestamp(raw)
        if isinstance(raw, _dt.date):
            return pd.Timestamp(raw)
    except Exception:
        pass
    s = str(raw).strip()
    cls = classify_value(s)
    if cls is None or not cls.canonical.is_temporal:
        return None
    dayfirst = cls.pattern in {"dmy_slash_unambiguous", "dmy_dash_unambiguous", "dmy_dot", "dmon_y"}
    try:
        ts = pd.to_datetime(s, dayfirst=dayfirst, errors="coerce")
    except Exception:
        return None
    return None if ts is pd.NaT or pd.isna(ts) else pd.Timestamp(ts)


def _numeric_stats(values: list[float]) -> NumericStats:
    if not values:
        return NumericStats()
    sv = sorted(values)
    def pct(p: float) -> float:
        if len(sv) == 1:
            return sv[0]
        k = (len(sv) - 1) * p
        lo, hi = math.floor(k), math.ceil(k)
        return sv[lo] if lo == hi else sv[lo] + (sv[hi] - sv[lo]) * (k - lo)
    return NumericStats(
        min=sv[0], max=sv[-1],
        mean=round(statistics.fmean(sv), 6),
        median=round(statistics.median(sv), 6),
        stddev=round(statistics.pstdev(sv), 6) if len(sv) > 1 else 0.0,
        p01=round(pct(0.01), 6), p25=round(pct(0.25), 6),
        p75=round(pct(0.75), 6), p99=round(pct(0.99), 6),
        negative_count=sum(1 for v in sv if v < 0),
        zero_count=sum(1 for v in sv if v == 0),
    )


def _find_outliers(series: pd.Series, numeric: NumericStats) -> list[Outlier]:
    """Robust outlier detection via modified z-score (median/MAD), which does
    not get dragged around by the very outliers we are looking for."""
    pairs = [(i, _to_float(v)) for i, v in enumerate(series.tolist())]
    vals = [(i, v) for i, v in pairs if v is not None]
    if len(vals) < 8:
        return []
    nums = [v for _, v in vals]
    med = statistics.median(nums)
    mad = statistics.median([abs(v - med) for v in nums]) or 0.0
    out: list[Outlier] = []
    if mad > 0:
        for i, v in vals:
            z = 0.6745 * (v - med) / mad
            if abs(z) >= OUTLIER_Z:
                out.append(Outlier(row_index=i, value=str(series.iloc[i]),
                                   reason=f"{abs(z):.1f} modified z-scores from the median ({med:g})",
                                   z_score=round(z, 3)))
    if numeric.negative_count and numeric.min is not None and numeric.min < 0:
        for i, v in vals:
            if v < 0 and len(out) < MAX_OUTLIERS and not any(o.row_index == i for o in out):
                out.append(Outlier(row_index=i, value=str(series.iloc[i]),
                                   reason="Negative value in an otherwise non-negative measure"))
    out.sort(key=lambda o: -abs(o.z_score or 0))
    return out[:MAX_OUTLIERS]


def profile_column(
    series: pd.Series, name: str, table_id: str, ordinal: int, row_count: int
) -> ColumnProfile:
    raw = series.tolist()
    nulls = sum(1 for v in raw if v is None or (isinstance(v, float) and math.isnan(v)))
    blanks = sum(
        1 for v in raw
        if v is not None and not (isinstance(v, float) and math.isnan(v)) and is_nullish(v)
    )
    populated = [v for v in raw if not is_nullish(v)]
    texts = [_as_text(v) for v in populated]
    counts = Counter(texts)
    unique = len(counts)

    verdict = classify_column(populated, name)
    prof = ColumnProfile(
        source_table_id=table_id, name=name, ordinal=ordinal,
        inferred_type=verdict.canonical,
        raw_pandas_dtype=str(series.dtype),
        row_count=row_count, null_count=nulls, blank_count=blanks,
        unique_count=unique,
        duplicate_count=max(0, len(populated) - unique),
        sample_values=[t for t, _ in counts.most_common(12)],
        top_values=[
            {"value": t, "count": c, "share": round(c / len(populated), 4)}
            for t, c in counts.most_common(10)
        ] if populated else [],
        detected_currency_symbols=sorted(verdict.currencies),
    )
    prof.type_confidence = (
        Confidence.HIGH if verdict.share >= 0.98
        else Confidence.MEDIUM if verdict.share >= LOW_TYPE_CONFORMANCE
        else Confidence.LOW
    )
    prof.format_variants = [
        FormatVariant(
            pattern=p, count=c,
            description=verdict.pattern_descriptions.get(p, ""),
            examples=verdict.pattern_examples.get(p, [])[:5],
        )
        for p, c in verdict.pattern_counts.most_common()
    ]

    if verdict.canonical.is_numeric:
        nums = [f for f in (_to_float(v) for v in populated) if f is not None]
        prof.numeric = _numeric_stats(nums)
        prof.outliers = _find_outliers(series, prof.numeric)
    elif verdict.canonical.is_temporal:
        dates = [d for d in (_to_date(v) for v in populated) if d is not None]
        if dates:
            now = pd.Timestamp.now()
            days = {d.normalize() for d in dates}
            if len({(d.year, d.month) for d in dates}) == len(days):
                gran = "month"
            elif len({d.year for d in dates}) == len(days):
                gran = "year"
            else:
                gran = "day"
            prof.temporal = TemporalStats(
                min_date=min(dates).date().isoformat(),
                max_date=max(dates).date().isoformat(),
                distinct_days=len(days),
                future_dated_count=sum(1 for d in dates if d > now),
                granularity=gran,
            )

    prof.is_complete = (nulls + blanks) == 0
    prof.is_unique = bool(populated) and unique == len(populated)
    prof.primary_key_score = _pk_score(prof)
    prof.issues = _column_issues(prof, verdict, len(populated))
    return prof


def _pk_score(p: ColumnProfile) -> float:
    """Evidence-based key candidacy. Name is a small nudge at most -- a column
    called 'id' that is 40% null scores badly, which is the point."""
    if not p.row_count:
        return 0.0
    completeness = 1.0 - p.null_share
    uniqueness = p.cardinality_ratio
    id_named = bool(ID_NAME_RE.search(p.name))
    # A plain integer column is only a plausible key if its *name* says so.
    # In a small sample many measures (qty, headcount) are accidentally
    # unique, and treating those as keys produces confidently wrong models.
    type_fit = {
        CanonicalType.IDENTIFIER: 1.0,
        CanonicalType.INTEGER: 0.85 if id_named else 0.55,
        CanonicalType.STRING: 0.6, CanonicalType.DATE: 0.2,
        CanonicalType.DATETIME: 0.2, CanonicalType.DECIMAL: 0.15,
        CanonicalType.CURRENCY: 0.05, CanonicalType.PERCENTAGE: 0.05,
        CanonicalType.BOOLEAN: 0.0, CanonicalType.EMPTY: 0.0,
        CanonicalType.MIXED: 0.3, CanonicalType.DURATION: 0.05,
    }.get(p.inferred_type, 0.4)
    name_bonus = 0.05 if id_named else 0.0
    score = (uniqueness ** 2) * completeness * type_fit + name_bonus
    if not (p.is_unique and p.is_complete):
        # Never show 1.00 for a column that is not actually a usable key --
        # a near-unique column reading "1.00" invites exactly the wrong call.
        return round(min(0.95, score), 4)
    return round(min(1.0, score), 4)


def _column_issues(p: ColumnProfile, verdict: Any, populated: int) -> list[Issue]:
    issues: list[Issue] = []
    loc = p.name

    if p.null_share >= CRITICAL_NULL_SHARE:
        issues.append(Issue(
            code="column.mostly_empty", severity=Severity.HIGH, location=loc,
            message=f"{p.null_share:.0%} of values are missing or blank.",
            evidence={"null_count": p.null_count, "blank_count": p.blank_count,
                      "row_count": p.row_count},
            recommended_fix="Confirm whether this column is in use; consider excluding it "
                            "from the model rather than modelling a near-empty field.",
        ))
    elif p.null_share >= HIGH_NULL_SHARE:
        issues.append(Issue(
            code="column.high_nulls", severity=Severity.MEDIUM, location=loc,
            message=f"{p.null_share:.0%} of values are missing or blank.",
            evidence={"null_count": p.null_count, "blank_count": p.blank_count},
            recommended_fix="Decide an explicit treatment (fill, flag, or exclude) before "
                            "aggregating over this column.",
        ))

    if verdict.is_mixed and verdict.share < LOW_TYPE_CONFORMANCE:
        issues.append(Issue(
            code="column.mixed_types", severity=Severity.HIGH, location=loc,
            message=f"Mixed value types: only {verdict.share:.0%} of values are "
                    f"{p.inferred_type.value}.",
            evidence={"type_counts": dict(verdict.type_counts)},
            recommended_fix=f"Add an explicit parse step to {p.inferred_type.value} and route "
                            "unparseable values to a quarantine column instead of coercing them.",
        ))

    real_formats = [f for f in p.format_variants if f.pattern != "text"]
    if len(real_formats) > 1 and (p.inferred_type.is_temporal or p.inferred_type.is_numeric):
        issues.append(Issue(
            code="column.inconsistent_format", severity=Severity.MEDIUM, location=loc,
            message=f"{len(real_formats)} different {p.inferred_type.value} formats are in use.",
            evidence={"formats": [
                {"pattern": f.pattern, "description": f.description,
                 "count": f.count, "examples": f.examples}
                for f in real_formats
            ]},
            recommended_fix="Normalise to a single representation during data preparation; the "
                            "generated Power Query M parses each detected format explicitly.",
        ))

    patterns = set(verdict.pattern_counts)
    if {"dmy_slash_unambiguous", "mdy_slash_unambiguous"} <= patterns or \
            {"dmy_dash_unambiguous", "mdy_slash_unambiguous"} <= patterns:
        dmy = verdict.pattern_counts.get("dmy_slash_unambiguous", 0) + \
            verdict.pattern_counts.get("dmy_dash_unambiguous", 0)
        mdy = verdict.pattern_counts.get("mdy_slash_unambiguous", 0)
        issues.append(Issue(
            code="column.conflicting_date_orders", severity=Severity.CRITICAL, location=loc,
            message=f"The column provably contains both day-first and month-first dates "
                    f"({dmy} value(s) can only be dd/mm, {mdy} can only be mm/dd).",
            evidence={
                "day_first_examples": verdict.pattern_examples.get("dmy_slash_unambiguous", [])
                or verdict.pattern_examples.get("dmy_dash_unambiguous", []),
                "month_first_examples": verdict.pattern_examples.get(
                    "mdy_slash_unambiguous", []),
                "day_first_count": dmy, "month_first_count": mdy,
            },
            recommended_fix="No single parse format is correct for this column. The rows must "
                            "be split by their detected format and parsed separately, which is "
                            "what the generated transformation does. Any row that is ambiguous "
                            "(day and month both <= 12) still needs the data owner to confirm "
                            "the intended order.",
        ))

    if verdict.ambiguous_patterns & {"slash_ambiguous", "dash_ambiguous"}:
        issues.append(Issue(
            code="column.ambiguous_date_order", severity=Severity.HIGH, location=loc,
            message="Dates are written as dd/mm or mm/dd and the order cannot be determined "
                    "from the data alone.",
            evidence={"examples": next(
                (f.examples for f in p.format_variants
                 if f.pattern in {"slash_ambiguous", "dash_ambiguous"}), [])},
            recommended_fix="Confirm the day/month order with the data owner. Parsing it the "
                            "wrong way round silently shifts up to 12/31 of all rows into the "
                            "wrong month.",
        ))

    if "excel_serial_candidate" in verdict.pattern_counts:
        issues.append(Issue(
            code="column.possible_excel_serial", severity=Severity.MEDIUM, location=loc,
            message="Values look like Excel date serial numbers rather than dates.",
            evidence={"examples": verdict.pattern_examples.get("excel_serial_candidate", [])},
            recommended_fix="Confirm whether these are dates; if so a parse step converts from "
                            "the 1899-12-30 epoch. They are left numeric until confirmed.",
        ))

    if len(p.detected_currency_symbols) > 1:
        issues.append(Issue(
            code="column.multiple_currencies", severity=Severity.CRITICAL, location=loc,
            message=f"More than one currency appears in the same column: "
                    f"{', '.join(p.detected_currency_symbols)}.",
            evidence={"currencies": p.detected_currency_symbols},
            recommended_fix="Summing mixed currencies produces a meaningless total. Split the "
                            "amount and currency into separate columns and convert to a single "
                            "reporting currency before aggregating.",
        ))

    if p.inferred_type is CanonicalType.EMPTY:
        issues.append(Issue(
            code="column.entirely_empty", severity=Severity.MEDIUM, location=loc,
            message="The column contains no values at all.",
            recommended_fix="Exclude it from the model.",
        ))

    # A column that is *almost* a key, with a handful of repeats, is one of the
    # most consequential findings there is: it is usually a conflicting record
    # rather than a benign duplicate, and it silently breaks a relationship.
    almost_a_key = p.cardinality_ratio >= NEAR_UNIQUE_RATIO or (
        p.duplicate_count <= MAX_ABSOLUTE_KEY_DUPES
        and p.cardinality_ratio >= MIN_KEY_CARDINALITY
    )
    if (
        p.inferred_type in {CanonicalType.IDENTIFIER, CanonicalType.INTEGER}
        and p.duplicate_count
        and almost_a_key
        and not p.is_unique
    ):
        repeats = [
            tv["value"] for tv in p.top_values
            if isinstance(tv.get("count"), int) and tv["count"] > 1
        ][:8]
        issues.append(Issue(
            code="column.duplicate_key_values", severity=Severity.HIGH, location=loc,
            message=f"{p.unique_count} distinct values across "
                    f"{p.row_count - p.null_count - p.blank_count} rows: this looks like a "
                    f"key, but {p.duplicate_count} value(s) repeat.",
            evidence={"repeated_examples": repeats, "unique_count": p.unique_count,
                      "duplicate_count": p.duplicate_count,
                      "cardinality_ratio": round(p.cardinality_ratio, 4)},
            recommended_fix="Check whether the repeated rows are identical (a true duplicate) "
                            "or differ in their other columns (a conflicting record). "
                            "Conflicting records must be adjudicated, not de-duplicated; "
                            "using this column as a relationship key while it repeats will "
                            "either fail in Power BI or fan out and double-count.",
        ))

    if p.outliers:
        issues.append(Issue(
            code="column.outliers", severity=Severity.LOW, location=loc,
            message=f"{len(p.outliers)} value(s) are far from the rest of the distribution.",
            evidence={"examples": [
                {"row": o.row_index, "value": o.value, "reason": o.reason}
                for o in p.outliers[:5]
            ]},
            recommended_fix="Review these rows; they are reported only, never removed.",
        ))

    if p.temporal and p.temporal.future_dated_count:
        issues.append(Issue(
            code="column.future_dates", severity=Severity.MEDIUM, location=loc,
            message=f"{p.temporal.future_dated_count} value(s) are in the future.",
            evidence={"max_date": p.temporal.max_date},
            recommended_fix="Confirm whether future dates are expected (forecasts, planned "
                            "milestones) before filtering them.",
        ))
    return issues


def _detect_composite_keys(
    frame: pd.DataFrame, profiles: list[ColumnProfile], row_count: int
) -> list[list[str]]:
    """Find minimal column sets that uniquely identify a row.

    Only columns that are complete and plausibly key-like are considered, and
    widths are capped, so this stays linear in practice rather than exploring
    the whole power set.
    """
    candidates = [
        p.name for p in profiles
        if p.is_complete and p.cardinality_ratio > 0.0
        and p.inferred_type in {
            CanonicalType.IDENTIFIER, CanonicalType.INTEGER, CanonicalType.STRING,
            CanonicalType.DATE, CanonicalType.DATETIME, CanonicalType.BOOLEAN,
        }
    ]
    candidates.sort(key=lambda n: -next(p.cardinality_ratio for p in profiles if p.name == n))
    candidates = candidates[:8]
    found: list[list[str]] = []

    singles = [
        p.name for p in profiles
        if p.is_unique and p.is_complete and p.primary_key_score >= PK_MIN_SCORE
    ]
    found.extend([s] for s in singles)

    if row_count == 0:
        return found

    # Composite candidates are searched even when a single-column key exists:
    # the natural key of a fact table (order + line) is informative about the
    # table's grain regardless of whether a surrogate id happens to be unique.
    from itertools import combinations
    composites: list[list[str]] = []
    for width in range(2, MAX_COMPOSITE_KEY_WIDTH + 1):
        if composites and width > 2:
            break
        for combo in combinations(candidates, width):
            if any(set(f) <= set(combo) for f in found + composites):
                continue
            try:
                n = frame[list(combo)].astype(str).drop_duplicates().shape[0]
            except Exception:
                continue
            if n == row_count:
                composites.append(list(combo))
                if len(composites) >= 3:
                    break
    found.extend(composites)
    return found


def profile_table(
    frame: pd.DataFrame,
    *,
    project_id: str,
    source_file_id: str,
    table_name: str,
    display_name: str = "",
    sheet_name: str = "",
    preview_limit: int = 50,
    reader_notes: list[str] | None = None,
    header_row_index: int = 0,
) -> SourceTable:
    row_count = int(frame.shape[0])
    table = SourceTable(
        project_id=project_id, source_file_id=source_file_id,
        sheet_name=sheet_name, table_name=table_name,
        display_name=display_name or sheet_name or table_name,
        row_count=row_count, column_count=int(frame.shape[1]),
        header_row_index=header_row_index,
    )
    table.columns = [
        profile_column(frame[col], str(col), table.id, i, row_count)
        for i, col in enumerate(frame.columns)
    ]

    if row_count:
        as_text = frame.astype(str)
        table.duplicate_row_count = int(row_count - as_text.drop_duplicates().shape[0])
        table.fully_empty_row_count = int(
            sum(1 for _, r in frame.iterrows() if all(is_nullish(v) for v in r.tolist()))
        )
    table.naming_convention = naming_convention([str(c) for c in frame.columns])
    table.candidate_primary_keys = _detect_composite_keys(frame, table.columns, row_count)
    table.preview_rows = [
        {str(k): _as_text(v) for k, v in rec.items()}
        for rec in frame.head(preview_limit).to_dict(orient="records")
    ]

    table.issues = _table_issues(table, reader_notes or [])
    from ...schemas.common import utcnow
    table.profiled_at = utcnow()
    return table


def _table_issues(t: SourceTable, reader_notes: list[str]) -> list[Issue]:
    issues: list[Issue] = []
    if t.row_count == 0:
        issues.append(Issue(
            code="table.empty", severity=Severity.HIGH, location=t.table_name,
            message="The table has headers but no data rows.",
            recommended_fix="Check the export; an empty fact table yields empty measures.",
        ))
    if t.duplicate_row_count:
        issues.append(Issue(
            code="table.duplicate_rows", severity=Severity.MEDIUM, location=t.table_name,
            message=f"{t.duplicate_row_count} fully duplicated row(s).",
            evidence={"duplicate_row_count": t.duplicate_row_count, "row_count": t.row_count},
            recommended_fix="Duplicates are reported, not removed: repeated rows are often "
                            "legitimate. Approve a de-duplication step only if they are not.",
        ))
    if t.fully_empty_row_count:
        issues.append(Issue(
            code="table.empty_rows", severity=Severity.LOW, location=t.table_name,
            message=f"{t.fully_empty_row_count} completely empty row(s).",
            recommended_fix="Safe to remove; proposed as a reviewable step.",
        ))
    if t.naming_convention == "mixed":
        issues.append(Issue(
            code="table.mixed_naming", severity=Severity.LOW, location=t.table_name,
            message="Column headers mix naming conventions.",
            evidence={"columns": t.column_names if hasattr(t, "column_names") else
                      [c.name for c in t.columns]},
            recommended_fix="Column mapping normalises these to canonical names.",
        ))
    if not t.candidate_primary_keys and t.row_count:
        issues.append(Issue(
            code="table.no_primary_key", severity=Severity.HIGH, location=t.table_name,
            message="No column or small column combination uniquely identifies a row.",
            evidence={"best_scores": sorted(
                ({"column": c.name, "score": c.primary_key_score} for c in t.columns),
                key=lambda d: -d["score"])[:5]},
            recommended_fix="Either the grain is coarser than one row per entity, or a "
                            "surrogate key is needed. A surrogate key step is proposed.",
        ))
    for note in reader_notes:
        if note.startswith(("Duplicate header", "Blank header", "A row had", "Row cap", "Header found")):
            issues.append(Issue(
                code="table.reader_note", severity=Severity.LOW, location=t.table_name,
                message=note,
            ))
    return issues
