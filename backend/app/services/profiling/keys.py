"""Cross-table key discovery.

The contract with the Semantic Model Agent is: *it may only propose a
relationship that a ``KeyOverlap`` here supports*. Name similarity is recorded
as one signal among several but is never sufficient on its own -- two columns
both called ``id`` with 2% value overlap must not become a relationship.

``match_share`` is computed on **distinct normalised values**, not rows, so a
fact table with 10,000 rows over 50 projects is not scored as "99% matched"
just because the popular projects all match.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from ...schemas.common import CanonicalType, Issue, Severity
from ...schemas.source import ColumnProfile, KeyOverlap, SourceTable
from .inference import CODE_LIKE_PATTERNS, ID_NAME_RE, is_nullish, name_similarity

#: Minimum share of a child column's distinct keys that must be found in the
#: parent before we will even show the pair as a candidate.
MIN_CANDIDATE_SHARE = 0.50
#: At or above this, a relationship is treated as evidentially sound.
STRONG_SHARE = 0.95
#: A value set no larger than this is treated as a category, not a key space.
CATEGORY_MAX_DISTINCT = 60
#: Columns with more distinct values than this are sampled for comparison.
COMPARE_LIMIT = 200_000


def normalise_key(value: object) -> str:
    """Normalisation used *only* for matching, never written back to data.

    Business systems disagree about case, padding, separators and leading
    zeros for the same identifier: ``PRJ-1001``, ``prj 1001``, ``PRJ1001`` and
    ``0001001`` frequently denote one project. Matching on the raw strings
    would report spurious orphans, so we compare on a normalised form and
    report the raw forms in the evidence.
    """
    if value is None or is_nullish(value):
        return ""
    s = str(value).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]                      # 1001.0 from a float round-trip
    s = re.sub(r"[\s_\-./]+", "", s).upper()
    m = re.fullmatch(r"([A-Z]*)0*(\d+)", s)
    if m and m.group(2):
        s = m.group(1) + m.group(2)     # strip leading zeros, keep any prefix
    return s


def _key_like(p: ColumnProfile) -> bool:
    if p.inferred_type in {
        CanonicalType.CURRENCY, CanonicalType.PERCENTAGE,
        CanonicalType.DECIMAL, CanonicalType.BOOLEAN, CanonicalType.EMPTY,
    }:
        return False
    if p.unique_count <= 1:
        return False
    if p.inferred_type in {CanonicalType.IDENTIFIER, CanonicalType.INTEGER}:
        return True
    if p.inferred_type is CanonicalType.STRING:
        # A text column is key-like when it is low-cardinality (a category),
        # named like an id, or its values are code-shaped. The last test
        # matters for small tables, where cardinality is 1.0 regardless.
        if ID_NAME_RE.search(p.name) or p.cardinality_ratio < 0.9:
            return True
        dominant = p.format_variants[0].pattern if p.format_variants else ""
        return dominant in CODE_LIKE_PATTERNS
    return bool(ID_NAME_RE.search(p.name))


@dataclass
class _ColView:
    table: str
    column: str
    profile: ColumnProfile
    distinct: set[str]
    raw_by_norm: dict[str, str]
    is_unique_normalised: bool


def _build_views(
    tables: list[SourceTable], frames: dict[str, pd.DataFrame]
) -> list[_ColView]:
    views: list[_ColView] = []
    for t in tables:
        frame = frames.get(t.table_name)
        if frame is None or frame.empty:
            continue
        for p in t.columns:
            if not _key_like(p) or p.name not in frame.columns:
                continue
            series = frame[p.name]
            if len(series) > COMPARE_LIMIT:
                series = series.head(COMPARE_LIMIT)
            raw_by_norm: dict[str, str] = {}
            norm_count = 0
            for v in series.tolist():
                n = normalise_key(v)
                if not n:
                    continue
                norm_count += 1
                raw_by_norm.setdefault(n, str(v))
            distinct = set(raw_by_norm)
            if not distinct:
                continue
            views.append(_ColView(
                table=t.table_name, column=p.name, profile=p,
                distinct=distinct, raw_by_norm=raw_by_norm,
                is_unique_normalised=len(distinct) == norm_count,
            ))
    return views


def find_key_overlaps(
    tables: list[SourceTable], frames: dict[str, pd.DataFrame]
) -> list[KeyOverlap]:
    """Measure the overlap of every plausible key pair across tables."""
    views = _build_views(tables, frames)
    overlaps: list[KeyOverlap] = []
    for i, a in enumerate(views):
        for b in views[i + 1:]:
            if a.table == b.table:
                continue
            at, bt = a.profile.inferred_type, b.profile.inferred_type
            type_compatible = (
                at == bt
                or {at, bt} <= {CanonicalType.IDENTIFIER, CanonicalType.INTEGER, CanonicalType.STRING}
            )
            shared = a.distinct & b.distinct
            if not shared:
                continue
            a_share = len(shared) / len(a.distinct)
            b_share = len(shared) / len(b.distinct)
            sim = name_similarity(a.column, b.column)
            # Keep a pair only if one side is substantially covered by the
            # other, or the names agree strongly enough to be worth showing.
            if max(a_share, b_share) < MIN_CANDIDATE_SHARE and sim < 0.6:
                continue
            overlaps.append(KeyOverlap(
                left=f"{a.table}.{a.column}", right=f"{b.table}.{b.column}",
                left_distinct=len(a.distinct), right_distinct=len(b.distinct),
                matched_distinct=len(shared),
                left_match_share=round(a_share, 4), right_match_share=round(b_share, 4),
                left_is_unique=a.is_unique_normalised, right_is_unique=b.is_unique_normalised,
                unmatched_left_examples=[a.raw_by_norm[n] for n in sorted(a.distinct - b.distinct)[:10]],
                unmatched_right_examples=[b.raw_by_norm[n] for n in sorted(b.distinct - a.distinct)[:10]],
                name_similarity=round(sim, 4), type_compatible=type_compatible,
            ))
    overlaps.sort(key=lambda o: -(max(o.left_match_share, o.right_match_share) + o.name_similarity / 4))
    return overlaps


def orient(overlap: KeyOverlap) -> tuple[str, str, str, str] | None:
    """Return (child_table, child_col, parent_table, parent_col) -- the
    many-side first, which is the direction Power BI relationships need.
    ``None`` when neither side is unique (a true many-to-many)."""
    lt, lc = overlap.left.split(".", 1)
    rt, rc = overlap.right.split(".", 1)
    if overlap.right_is_unique and not overlap.left_is_unique:
        return lt, lc, rt, rc
    if overlap.left_is_unique and not overlap.right_is_unique:
        return rt, rc, lt, lc
    if overlap.left_is_unique and overlap.right_is_unique:
        # 1:1 -- orient by which side looks more like a dimension (fewer rows)
        return (lt, lc, rt, rc) if overlap.left_distinct >= overlap.right_distinct else (rt, rc, lt, lc)
    return None


def cross_table_issues(
    tables: list[SourceTable], overlaps: list[KeyOverlap]
) -> list[Issue]:
    """Findings that only exist between tables: orphan keys, many-to-many
    risk, conflicting definitions of the same concept."""
    issues: list[Issue] = []

    for o in overlaps:
        oriented = orient(o)
        if oriented is None:
            if max(o.left_match_share, o.right_match_share) < MIN_CANDIDATE_SHARE:
                continue
            both_small = max(o.left_distinct, o.right_distinct) <= CATEGORY_MAX_DISTINCT
            near_total = min(o.left_match_share, o.right_match_share) >= STRONG_SHARE
            if both_small and near_total:
                # Two tables carrying the same small value set: this is a shared
                # attribute wanting a conformed dimension, not a join hazard.
                issues.append(Issue(
                    code="keys.shared_category", severity=Severity.LOW,
                    location=f"{o.left} <-> {o.right}",
                    message=f"Both columns carry the same set of "
                            f"{o.matched_distinct} values, so they are the same attribute "
                            f"recorded in two places.",
                    evidence={"matched_distinct": o.matched_distinct,
                              "left_distinct": o.left_distinct,
                              "right_distinct": o.right_distinct},
                    recommended_fix="Build one conformed dimension from the distinct values and "
                                    "relate both tables to it, so a slicer filters both "
                                    "consistently.",
                ))
            else:
                issues.append(Issue(
                    code="keys.many_to_many", severity=Severity.MEDIUM,
                    location=f"{o.left} <-> {o.right}",
                    message=f"Neither side of this key pair is unique "
                            f"({o.left_distinct} vs {o.right_distinct} distinct values), so "
                            f"joining them directly would be many-to-many.",
                    evidence={"matched_distinct": o.matched_distinct,
                              "left_share": o.left_match_share,
                              "right_share": o.right_match_share},
                    recommended_fix="If these are two fact tables, append them or relate both to "
                                    "a shared dimension rather than to each other. If a join is "
                                    "really needed, add a bridge table of the distinct keys or "
                                    "aggregate one side to its grain first -- a direct join "
                                    "duplicates rows and inflates every additive measure.",
                ))
            continue

        ct, cc, pt, pc = oriented
        child_share = o.left_match_share if f"{ct}.{cc}" == o.left else o.right_match_share
        unmatched = (
            o.unmatched_left_examples if f"{ct}.{cc}" == o.left else o.unmatched_right_examples
        )
        child_distinct = o.left_distinct if f"{ct}.{cc}" == o.left else o.right_distinct
        orphans = round(child_distinct * (1 - child_share))
        if child_share < 1.0 and orphans > 0 and child_share >= MIN_CANDIDATE_SHARE:
            sev = (
                Severity.CRITICAL if child_share < 0.8
                else Severity.HIGH if child_share < STRONG_SHARE
                else Severity.MEDIUM
            )
            issues.append(Issue(
                code="keys.unmatched_identifiers", severity=sev,
                location=f"{ct}.{cc} -> {pt}.{pc}",
                message=f"{orphans} of {child_distinct} distinct keys in {ct}.{cc} "
                        f"({1 - child_share:.1%}) have no match in {pt}.{pc}.",
                evidence={"match_share": child_share, "unmatched_examples": unmatched[:10],
                          "child_distinct": child_distinct},
                recommended_fix="Rows with unmatched keys fall outside the dimension and will be "
                                "excluded from any sliced total. Either add the missing dimension "
                                "members or add an explicit 'Unknown' member so the rows remain "
                                "visible.",
            ))
        if not o.type_compatible:
            issues.append(Issue(
                code="keys.type_mismatch", severity=Severity.MEDIUM,
                location=f"{o.left} <-> {o.right}",
                message="The two key columns have incompatible inferred types "
                        f"({o.left.split('.')[-1]} vs {o.right.split('.')[-1]}).",
                evidence={"left_type": "", "right_type": ""},
                recommended_fix="Cast both sides to the same type during preparation; Power BI "
                                "will not create a relationship across mismatched types.",
            ))

    # Same concept, conflicting value sets -> likely conflicting definitions.
    by_concept: dict[str, list[tuple[str, ColumnProfile]]] = {}
    from .inference import canonicalise_name
    for t in tables:
        for c in t.columns:
            if c.inferred_type in {CanonicalType.STRING, CanonicalType.IDENTIFIER} and \
                    2 <= c.unique_count <= 60:
                by_concept.setdefault(canonicalise_name(c.name), []).append((t.table_name, c))
    for concept, entries in by_concept.items():
        if len(entries) < 2:
            continue
        value_sets = {
            tn: {str(v["value"]).strip().lower() for v in c.top_values} for tn, c in entries
        }
        names = list(value_sets)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = value_sets[names[i]], value_sets[names[j]]
                if not a or not b:
                    continue
                overlap_share = len(a & b) / len(a | b)
                if overlap_share < 0.34:
                    issues.append(Issue(
                        code="keys.conflicting_category_values", severity=Severity.MEDIUM,
                        location=f"{names[i]}.{concept} vs {names[j]}.{concept}",
                        message=f"'{concept}' uses largely different value sets in "
                                f"{names[i]} and {names[j]} (only "
                                f"{overlap_share:.0%} of values are shared).",
                        evidence={names[i]: sorted(a)[:8], names[j]: sorted(b)[:8]},
                        recommended_fix="These may be different vocabularies for the same "
                                        "concept. Confirm the mapping before using either as a "
                                        "shared dimension; a category mapping step is proposed.",
                    ))
    return issues
