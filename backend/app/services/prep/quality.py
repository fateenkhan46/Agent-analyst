"""Data-quality measurement, before and after transformation.

Both reports are computed by the same function over the same metrics, so the
comparison is meaningful: a change in the score reflects a change in the data,
not a change in how it was measured.
"""
from __future__ import annotations

import pandas as pd

from ...schemas.common import Issue, Severity
from ...schemas.prep import DataQualityReport
from ..profiling.inference import classify_value, is_nullish
from ..profiling.keys import normalise_key


def measure(
    project_id: str, phase: str, frames: dict[str, pd.DataFrame],
    *, key_pairs: list[tuple[str, str]] | None = None,
) -> DataQualityReport:
    """Measure quality metrics over a set of frames.

    ``key_pairs`` are ``("child.col", "parent.col")`` references whose
    referential integrity should be checked, so the orphan-key count is
    comparable before and after.
    """
    report = DataQualityReport(project_id=project_id, phase=phase)

    for name, frame in frames.items():
        report.table_row_counts[name] = int(len(frame))
        report.table_column_counts[name] = int(len(frame.columns))
        if len(frame):
            report.duplicate_row_counts[name] = int(
                len(frame) - frame.astype(str).drop_duplicates().shape[0]
            )
        else:
            report.duplicate_row_counts[name] = 0

        for col in frame.columns:
            ref = f"{name}.{col}"
            # Audit and quarantine columns hold the pre-transformation value
            # on purpose, so they are heterogeneous by design. Measuring type
            # conformance on them reports a problem that is actually the
            # feature working.
            if str(col).endswith(("_original", "_unparsed", "_source_format")):
                continue
            series = frame[col]
            total = len(series)
            if not total:
                report.null_shares[ref] = 0.0
                report.type_conformance[ref] = 1.0
                continue
            missing = sum(1 for v in series.tolist() if is_nullish(v))
            report.null_shares[ref] = round(missing / total, 6)

            populated = [v for v in series.tolist() if not is_nullish(v)]
            if not populated:
                report.type_conformance[ref] = 1.0
                report.format_variant_counts[ref] = 0
                continue
            # Conformance: the share of values sharing the dominant canonical
            # type. After a successful parse this should be 1.0.
            kinds: dict[str, int] = {}
            patterns: set[str] = set()
            for v in populated[:5000]:
                cls = classify_value(v, col)
                if cls is None:
                    continue
                kinds[cls.canonical.value] = kinds.get(cls.canonical.value, 0) + 1
                patterns.add(cls.pattern)
            if kinds:
                dominant = max(kinds.values())
                report.type_conformance[ref] = round(dominant / sum(kinds.values()), 6)
            else:
                report.type_conformance[ref] = 1.0
            report.format_variant_counts[ref] = len(patterns)

    for child_ref, parent_ref in key_pairs or []:
        ct, cc = child_ref.split(".", 1)
        pt, pc = parent_ref.split(".", 1)
        child, parent = frames.get(ct), frames.get(pt)
        if child is None or parent is None or cc not in child.columns or pc not in parent.columns:
            continue
        parent_keys = {normalise_key(v) for v in parent[pc].tolist()} - {""}
        child_keys = {normalise_key(v) for v in child[cc].tolist()} - {""}
        orphans = child_keys - parent_keys
        report.orphan_key_counts[f"{child_ref}->{parent_ref}"] = len(orphans)

    report.issues = _issues(report)
    return report


def _issues(report: DataQualityReport) -> list[Issue]:
    issues: list[Issue] = []
    for ref, share in report.null_shares.items():
        if share >= 0.5:
            issues.append(Issue(
                code="quality.high_nulls", severity=Severity.MEDIUM, location=ref,
                message=f"{share:.0%} of values are missing.",
                evidence={"null_share": share},
                recommended_fix="Decide an explicit treatment before aggregating.",
            ))
    for ref, conformance in report.type_conformance.items():
        if conformance < 0.95:
            issues.append(Issue(
                code="quality.type_nonconformance", severity=Severity.HIGH, location=ref,
                message=f"Only {conformance:.0%} of values share the dominant type.",
                evidence={"conformance": conformance},
                recommended_fix="Add or correct a parse step for this column.",
            ))
    for ref, count in report.orphan_key_counts.items():
        if count:
            issues.append(Issue(
                code="quality.orphan_keys", severity=Severity.HIGH, location=ref,
                message=f"{count} key(s) have no match in the parent table.",
                evidence={"orphan_count": count},
                recommended_fix="Add the missing dimension members, or an explicit "
                                "'UNASSIGNED' member so the rows stay in the totals.",
            ))
    for table, count in report.duplicate_row_counts.items():
        if count:
            issues.append(Issue(
                code="quality.duplicate_rows", severity=Severity.MEDIUM, location=table,
                message=f"{count} fully duplicated row(s).",
                evidence={"duplicate_rows": count},
                recommended_fix="Confirm whether the repeats are legitimate before "
                                "de-duplicating.",
            ))
    return issues


def compare(before: DataQualityReport, after: DataQualityReport) -> dict[str, object]:
    """A side-by-side summary, including the metrics that got *worse*.

    Reporting regressions matters: a parse step that quarantines half a column
    improves type conformance while destroying completeness, and that trade
    must be visible rather than hidden behind a single improved score.
    """
    improved: list[str] = []
    regressed: list[str] = []

    for ref, after_value in after.type_conformance.items():
        before_value = before.type_conformance.get(ref)
        if before_value is None:
            continue
        if after_value > before_value + 0.001:
            improved.append(
                f"{ref}: type conformance {before_value:.0%} -> {after_value:.0%}"
            )
        elif after_value < before_value - 0.001:
            regressed.append(
                f"{ref}: type conformance {before_value:.0%} -> {after_value:.0%}"
            )
    for ref, after_value in after.null_shares.items():
        before_value = before.null_shares.get(ref)
        if before_value is None:
            continue
        if after_value > before_value + 0.001:
            regressed.append(
                f"{ref}: missing values {before_value:.0%} -> {after_value:.0%}"
            )
        elif after_value < before_value - 0.001:
            improved.append(f"{ref}: missing values {before_value:.0%} -> {after_value:.0%}")
    for key, after_count in after.orphan_key_counts.items():
        before_count = before.orphan_key_counts.get(key)
        if before_count is None:
            continue
        if after_count < before_count:
            improved.append(f"{key}: orphan keys {before_count} -> {after_count}")
        elif after_count > before_count:
            regressed.append(f"{key}: orphan keys {before_count} -> {after_count}")

    # Row totals are only comparable over tables present in BOTH phases:
    # a generated calendar adds thousands of rows and would otherwise look
    # like the transformation invented data.
    shared = set(before.table_row_counts) & set(after.table_row_counts)
    generated = sorted(set(after.table_row_counts) - set(before.table_row_counts))
    return {
        "score_before": before.score,
        "score_after": after.score,
        "delta": round(after.score - before.score, 4),
        "rows_before": sum(before.table_row_counts[t] for t in shared),
        "rows_after": sum(after.table_row_counts[t] for t in shared),
        "comparable_tables": sorted(shared),
        "generated_tables": {
            t: after.table_row_counts[t] for t in generated
        },
        "rows_total_after": sum(after.table_row_counts.values()),
        "improved": improved[:40],
        "regressed": regressed[:40],
        "issues_before": len(before.issues),
        "issues_after": len(after.issues),
    }
