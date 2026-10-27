"""Planning the data preparation.

The planner reads the measured ``SourceDataProfile`` and emits a
``TransformationPlan``: canonical column mappings, an ordered list of typed
steps, and join proposals.

Three principles are enforced here:

1. **Evidence, not names.** A join is proposed only from a measured
   ``KeyOverlap``; the name similarity is recorded as context, never as the
   reason.
2. **Nothing destructive without consent.** Deduplication and row filters are
   emitted as steps that require approval, with the row impact stated. Rows
   are never dropped to make a later step tidier.
3. **Repair, don't coerce.** A column with several date formats gets a step
   that parses *each detected format* and routes failures to a quarantine
   column, rather than a blanket cast that silently nulls what it cannot read.
"""
from __future__ import annotations

import re
from typing import Any

from ...schemas.common import CanonicalType, Confidence, Origin, Provenance, Severity
from ...schemas.prep import (
    ColumnMapping, DuplicateFinding, JoinProposal, Risk, TransformOp,
    TransformationPlan, TransformationStep,
)
from ...schemas.requirements import RequirementsSpec
from ...schemas.source import ColumnProfile, SourceDataProfile, SourceTable
from .. import kpi_catalog
from ..profiling.inference import canonicalise_name
from ..profiling.keys import MIN_CANDIDATE_SHARE, STRONG_SHARE, orient

#: A fact table is one whose rows are events: it has a measure and a date, and
#: substantially more rows than its key cardinality.
FACT_MIN_ROW_RATIO = 1.5


#: Canonical name per semantic role. Applied only when the source name is
#: generic enough that the role name is more informative.
ROLE_CANONICAL_NAME = {
    "cost": "cost_amount", "budget": "approved_budget", "hours": "labour_hours",
    "completion": "percent_complete", "project_key": "project_id",
    "employee_key": "employee_id", "date": "transaction_date",
    "category": "cost_category", "status": "status", "portfolio": "portfolio",
    "manager": "project_manager", "vendor": "vendor_id", "rate": "hourly_rate",
    "department": "department", "invoice": "invoice_no", "name": "name",
}

#: Source names so generic that the role's canonical name is clearer. Anything
#: else keeps its own meaning: a table can hold several dates ("start_date",
#: "planned_finish", "week_ending") and collapsing them all to
#: "transaction_date" both loses information and collides.
GENERIC_SOURCE_NAMES: dict[str, frozenset[str]] = {
    "date": frozenset({
        "date", "dates", "cost_date", "transaction_date", "txn_date", "posting_date",
        "doc_date", "period", "period_date", "entry_date",
    }),
    "cost": frozenset({
        "cost", "amount", "value", "cost_amount", "amount_usd", "actual_cost",
        "spend", "expense", "total",
    }),
    "budget": frozenset({"budget", "approved_budget", "planned_cost", "baseline"}),
    "hours": frozenset({"hours", "hours_worked", "labour_hours", "labor_hours", "effort"}),
    "project_key": frozenset({
        "project", "project_id", "proj_id", "project_code", "proj", "project_number",
        "project_no",
    }),
    "employee_key": frozenset({"employee", "employee_id", "emp_id", "staff_id", "resource"}),
    "invoice": frozenset({"invoice", "invoice_no", "invoice_number", "document_no"}),
    "vendor": frozenset({"vendor", "supplier", "vendor_id", "supplier_id"}),
    "name": frozenset({"name", "description", "title"}),
    "rate": frozenset({"rate", "hourly_rate", "unit_price"}),
}


def _canonical_for(column: ColumnProfile, table: SourceTable) -> tuple[str, str, Confidence, str]:
    """Return (canonical_name, role, confidence, reason)."""
    role = kpi_catalog.role_for_column(column.name)
    canon = canonicalise_name(column.name)
    if not role:
        return (
            canon, "", Confidence.MEDIUM,
            f"No semantic role recognised; normalised '{column.name}' to snake_case.",
        )

    preferred = ROLE_CANONICAL_NAME.get(role, canon)
    generic = GENERIC_SOURCE_NAMES.get(role)
    if generic is not None and canon not in generic:
        # The source name carries meaning the role name would lose.
        return (
            canon, role, Confidence.HIGH,
            f"Column '{column.name}' has the '{role}' role, but its own name is more "
            f"specific than the generic '{preferred}', so it is kept as '{canon}'.",
        )
    return (
        preferred, role, Confidence.HIGH,
        f"Column name '{column.name}' matches the '{role}' role vocabulary.",
    )


def _build_mappings(
    profile: SourceDataProfile, overrides: dict[str, str]
) -> list[ColumnMapping]:
    """Map every source column to a canonical name, per table.

    Two columns in one table must never receive the same canonical name: the
    rename step would then collide and one column's data would be lost. A
    collision is resolved with a numeric suffix and the reason records it, so
    the reviewer can give it a proper name.
    """
    mappings: list[ColumnMapping] = []
    for table in profile.tables:
        taken: set[str] = set()
        for column in table.columns:
            key = f"{table.table_name}.{column.name}"
            canon, role, confidence, reason = _canonical_for(column, table)
            override = overrides.get(key)
            if override:
                canon, confidence = override, Confidence.HIGH
                reason = "Set explicitly by the user."
            if canon in taken:
                # Prefer the column's own name over a numeric suffix: in a
                # table with both "employee_id" and "employee", the second is
                # the employee's *name*, so "employee" is far more useful than
                # "employee_id_2".
                collided_with = canon
                own = canonicalise_name(column.name)
                if own not in taken:
                    canon = own
                    reason = (
                        f"'{collided_with}' is already used in this table, so the column "
                        f"keeps its own name '{own}' -- the two columns hold different "
                        f"things despite sharing a semantic role."
                    )
                    confidence = Confidence.MEDIUM
                else:
                    base, n = canon, 2
                    while canon in taken:
                        canon = f"{base}_{n}"
                        n += 1
                    reason = (
                        f"{reason} Another column in this table already maps to "
                        f"'{base}' and so does this column's own name, so it was given a "
                        f"distinct name to avoid losing either column. Please rename it."
                    )
                    confidence = Confidence.LOW
            taken.add(canon)

            unit_conversion = ""
            if role == "hours":
                patterns = {f.pattern for f in column.format_variants}
                if patterns & {"duration_unit", "duration_hhmm"}:
                    unit_conversion = "minutes/hh:mm -> decimal hours"
            mappings.append(ColumnMapping(
                source_table=table.table_name, source_column=column.name,
                canonical_name=canon, canonical_type=column.inferred_type,
                confidence=confidence, reason=reason,
                accepted=True if override else None,
                unit_conversion=unit_conversion,
                evidence=[
                    f"inferred type {column.inferred_type.value} "
                    f"({column.type_confidence.value} confidence)",
                    f"{column.unique_count} distinct of "
                    f"{column.row_count - column.null_count - column.blank_count} populated",
                ] + ([f"semantic role: {role}"] if role else []),
                alternatives=[canonicalise_name(column.name)] if canon != canonicalise_name(
                    column.name) else [],
            ))
    return mappings


def _append_groups(profile: SourceDataProfile, mappings: list[ColumnMapping]) -> list[list[str]]:
    """Tables that describe the same thing under different headers, and should
    therefore be appended rather than joined.

    Detected by canonical-column overlap: two tables whose mapped column sets
    agree on at least ~70% of their columns are the same entity.
    """
    canon_by_table: dict[str, set[str]] = {}
    for m in mappings:
        canon_by_table.setdefault(m.source_table, set()).add(m.canonical_name)

    names = sorted(canon_by_table)
    groups: list[list[str]] = []
    used: set[str] = set()
    for i, a in enumerate(names):
        if a in used:
            continue
        group = [a]
        for b in names[i + 1:]:
            if b in used:
                continue
            sa, sb = canon_by_table[a], canon_by_table[b]
            if not sa or not sb:
                continue
            overlap = len(sa & sb) / min(len(sa), len(sb))
            if overlap >= 0.70:
                group.append(b)
                used.add(b)
        if len(group) > 1:
            used.add(a)
            groups.append(group)
    return groups


def _dataset_name(tables: list[str]) -> str:
    """A readable dataset name for an appended group."""
    if len(tables) == 1:
        return tables[0]
    parts = [t.split("_") for t in tables]
    common = []
    for tokens in zip(*parts):
        if len({t for t in tokens}) == 1:
            common.append(tokens[0])
        else:
            break
    base = "_".join(common) or tables[0]
    return re.sub(r"_+", "_", base).strip("_") or "combined"


def _classify_role(table: SourceTable, mappings: list[ColumnMapping]) -> str:
    """fact | dimension -- from measured shape, not from the table's name."""
    by_col = {m.source_column: m for m in mappings if m.source_table == table.table_name}
    roles = {m.canonical_name: kpi_catalog.role_for_column(m.source_column)
             for m in by_col.values()}
    has_measure = any(r in kpi_catalog.MEASURE_ROLES for r in roles.values())
    has_date = any(r == "date" for r in roles.values())
    key_cols = [c for c, r in roles.items() if r in kpi_catalog.KEY_ROLES]
    # A dimension has one row per entity: its key is unique.
    unique_key = any(
        (col := table.column(m.source_column)) is not None and col.is_unique
        for m in by_col.values()
        if kpi_catalog.role_for_column(m.source_column) in kpi_catalog.KEY_ROLES
    )
    if has_measure and (has_date or not unique_key):
        return "fact"
    if unique_key and not has_measure:
        return "dimension"
    if unique_key and has_measure and not has_date:
        # e.g. the project register: one row per project, carrying a budget.
        return "dimension"
    return "fact" if has_measure else "dimension"


def plan_transformations(
    *, profile: Any, requirements: Any = None, project_id: str,
    user_overrides: dict[str, str] | None = None,
) -> TransformationPlan:
    profile = (
        profile if isinstance(profile, SourceDataProfile)
        else SourceDataProfile.model_validate(profile)
    )
    spec: RequirementsSpec | None = None
    if requirements is not None:
        spec = (
            requirements if isinstance(requirements, RequirementsSpec)
            else RequirementsSpec.model_validate(requirements)
        )

    mappings = _build_mappings(profile, user_overrides or {})
    steps: list[TransformationStep] = []
    ordinal = 0

    def add(
        op: TransformOp, table: str, params: dict[str, Any], *, title: str, reason: str,
        effect: str, evidence: list[str] | None = None, risk: Risk = Risk.SAFE,
        columns: list[str] | None = None, confidence: Confidence = Confidence.HIGH,
    ) -> TransformationStep:
        nonlocal ordinal
        ordinal += 1
        step = TransformationStep(
            ordinal=ordinal, op=op, target_table=table, params=params, title=title,
            reason=reason, expected_effect=effect, evidence=evidence or [],
            affects_columns=columns or [], confidence=confidence,
        )
        if risk is not Risk.SAFE and step.risk is Risk.SAFE:
            step.risk = risk
        steps.append(step)
        return step

    # ------------------------------------------------------------------
    # 1. Per-table cleaning, in a deliberate order:
    #    rename -> trim -> parse -> flag -> derive
    # ------------------------------------------------------------------
    for table in profile.tables:
        tname = table.table_name
        table_maps = [m for m in mappings if m.source_table == tname]

        renames = {
            m.source_column: m.canonical_name
            for m in table_maps if m.source_column != m.canonical_name
        }
        if renames:
            add(
                TransformOp.RENAME_COLUMN, tname, {"renames": renames},
                title=f"Standardise {len(renames)} column name(s)",
                reason="The sources use different headers for the same concepts; canonical "
                       "names let them be appended and modelled together.",
                effect=f"Renames {len(renames)} column(s). No values change.",
                evidence=[f"'{k}' -> '{v}'" for k, v in list(renames.items())[:8]],
                columns=list(renames.values()),
            )

        if table.fully_empty_row_count:
            add(
                TransformOp.REMOVE_EMPTY_ROWS, tname, {},
                title=f"Remove {table.fully_empty_row_count} completely empty row(s)",
                reason="Rows with no value in any column carry no information and would "
                       "appear as blank members in every dimension.",
                effect=f"Removes {table.fully_empty_row_count} of {table.row_count} rows.",
                evidence=[f"{table.fully_empty_row_count} rows are empty in every column"],
            )

        for m in table_maps:
            col = table.column(m.source_column)
            if col is None:
                continue
            canon = m.canonical_name
            role = kpi_catalog.role_for_column(m.source_column)

            # whitespace: only where it actually varies
            if col.inferred_type in {CanonicalType.STRING, CanonicalType.IDENTIFIER}:
                if any(
                    str(v.get("value", "")) != str(v.get("value", "")).strip()
                    or "  " in str(v.get("value", ""))
                    for v in col.top_values
                ):
                    add(
                        TransformOp.TRIM_WHITESPACE, tname,
                        {"column": canon, "collapse_internal": True},
                        title=f"Normalise whitespace in {canon}",
                        reason="Leading, trailing or doubled spaces split what should be one "
                               "value into several distinct members.",
                        effect="Trims and collapses whitespace; the distinct count may fall.",
                        evidence=[
                            f"example: {v['value']!r}" for v in col.top_values[:3]
                            if str(v.get("value", "")) != str(v.get("value", "")).strip()
                            or "  " in str(v.get("value", ""))
                        ][:3],
                        columns=[canon],
                    )

            # identifier normalisation where a key is written inconsistently
            if role in kpi_catalog.KEY_ROLES:
                styles = {f.pattern for f in col.format_variants}
                inconsistent = len(styles) > 1 or any(
                    re.search(r"\s", str(v.get("value", ""))) for v in col.top_values
                )
                if inconsistent:
                    add(
                        TransformOp.NORMALISE_IDENTIFIER, tname,
                        {"column": canon, "upper": True, "strip_separators": True,
                         "strip_leading_zeros": True},
                        title=f"Normalise the {canon} identifier format",
                        reason="The same identifier is written differently across systems "
                               "(case, spaces, separators, leading zeros), which breaks the "
                               "relationship to the dimension.",
                        effect="Rewrites the key to a single canonical form. A "
                               "'<name>_original' column preserves the source value.",
                        evidence=[f"formats present: {', '.join(sorted(styles))}"],
                        columns=[canon],
                    )

            # dates: parse each detected format rather than guessing one
            if col.inferred_type.is_temporal:
                formats = [
                    {"pattern": f.pattern, "count": f.count, "examples": f.examples}
                    for f in col.format_variants
                ]
                ambiguous = any(
                    f["pattern"] in {"slash_ambiguous", "dash_ambiguous"} for f in formats
                )
                conflicting = {"dmy_slash_unambiguous", "mdy_slash_unambiguous"} <= {
                    f["pattern"] for f in formats
                }
                step = add(
                    TransformOp.PARSE_DATE, tname,
                    {"column": canon, "formats": formats,
                     "dayfirst_default": True, "quarantine_column": f"{canon}_unparsed"},
                    title=f"Parse {canon} from {len(formats)} detected format(s)",
                    reason="The column mixes date formats. Each detected format is parsed "
                           "explicitly; anything unparseable is kept in a quarantine column "
                           "instead of becoming a silent null.",
                    effect=f"Produces a real date column. {len(formats)} format(s) handled.",
                    evidence=[
                        f"{f['pattern']}: {f['count']} value(s), e.g. "
                        f"{', '.join(f['examples'][:2])}" for f in formats[:4]
                    ],
                    columns=[canon],
                    confidence=Confidence.LOW if (ambiguous or conflicting) else Confidence.HIGH,
                )
                if ambiguous or conflicting:
                    step.reason += (
                        " The day/month order cannot be determined from the data for some "
                        "values, so the assumed order needs confirming."
                    )

            # numbers / currency / percentage / duration
            if col.inferred_type is CanonicalType.CURRENCY or (
                role in {"cost", "budget", "rate"} and col.inferred_type.is_numeric
            ):
                add(
                    TransformOp.PARSE_CURRENCY, tname,
                    {"column": canon, "currencies": col.detected_currency_symbols,
                     "expected_currency": (spec.currency if spec else "") or "",
                     "quarantine_column": f"{canon}_unparsed",
                     "currency_column": f"{canon}_currency"},
                    title=f"Parse {canon} to a numeric amount",
                    reason="Amounts are written with symbols, thousands separators, "
                           "accounting parentheses and mixed decimal marks.",
                    effect="Produces a numeric column plus a currency column. Amounts in a "
                           "currency other than the reporting currency are retained but "
                           "excluded from the reporting-currency total.",
                    evidence=[
                        f"{f.pattern}: {f.count} value(s)" for f in col.format_variants[:4]
                    ] + ([f"currencies detected: {', '.join(col.detected_currency_symbols)}"]
                         if len(col.detected_currency_symbols) > 1 else []),
                    columns=[canon],
                    confidence=Confidence.LOW if len(col.detected_currency_symbols) > 1
                    else Confidence.HIGH,
                )
            elif col.inferred_type is CanonicalType.PERCENTAGE:
                add(
                    TransformOp.PARSE_PERCENTAGE, tname,
                    {"column": canon, "store_as_fraction": True},
                    title=f"Parse {canon} to a fraction",
                    reason="Percentages are stored as text with a % sign.",
                    effect="Produces a 0..1 fraction; Power BI formats it as a percentage.",
                    evidence=[f"example: {v['value']}" for v in col.top_values[:2]],
                    columns=[canon],
                )
            elif col.inferred_type is CanonicalType.DURATION or role == "hours":
                patterns = sorted({f.pattern for f in col.format_variants})
                add(
                    TransformOp.PARSE_NUMBER, tname,
                    {"column": canon, "unit_normalisation": "hours",
                     "formats": patterns, "quarantine_column": f"{canon}_unparsed",
                     "audit_column": f"{canon}_source_format"},
                    title=f"Normalise {canon} to decimal hours",
                    reason="Hours are recorded in several units (decimal, minutes, hh:mm); "
                           "summing them as-is would be meaningless.",
                    effect="Converts every value to decimal hours and records the original "
                           "format in an audit column so changed rows are identifiable.",
                    evidence=[f"formats present: {', '.join(patterns)}"],
                    columns=[canon],
                )
            elif col.inferred_type.is_numeric and col.type_confidence is Confidence.LOW:
                add(
                    TransformOp.PARSE_NUMBER, tname,
                    {"column": canon, "quarantine_column": f"{canon}_unparsed"},
                    title=f"Parse {canon} to a number",
                    reason="The column is mostly numeric but contains values that will not "
                           "parse.",
                    effect="Produces a numeric column; unparseable values are quarantined.",
                    columns=[canon],
                )

            # missing values on a key: flag, never fill
            if role in kpi_catalog.KEY_ROLES and (col.null_count + col.blank_count):
                add(
                    TransformOp.FILL_MISSING, tname,
                    {"column": canon, "value": "UNASSIGNED", "flag_column": f"{canon}_is_missing"},
                    title=f"Replace {col.null_count + col.blank_count} missing "
                          f"{canon} value(s) with 'UNASSIGNED'",
                    reason="Rows with no key would be excluded from every sliced total and "
                           "the figures would not tie back to the source. An explicit "
                           "'UNASSIGNED' member keeps them visible.",
                    effect=f"{col.null_count + col.blank_count} row(s) get the key "
                           f"'UNASSIGNED' and a flag column marking them.",
                    evidence=[
                        f"{col.null_count} null and {col.blank_count} blank value(s) "
                        f"of {col.row_count} rows"
                    ],
                    columns=[canon],
                )
            elif (col.null_count + col.blank_count) and role in kpi_catalog.MEASURE_ROLES:
                add(
                    TransformOp.FLAG_MISSING, tname,
                    {"column": canon, "flag_column": f"{canon}_is_missing"},
                    title=f"Flag {col.null_count + col.blank_count} missing {canon} value(s)",
                    reason="A missing measure is not a zero. Flagging keeps the row and lets "
                           "the report distinguish 'no data' from 'nothing spent'.",
                    effect="Adds a boolean flag column. No value is imputed.",
                    columns=[canon],
                )

    # ------------------------------------------------------------------
    # 2. Duplicates: reported, and only removed with approval
    # ------------------------------------------------------------------
    duplicate_findings: list[DuplicateFinding] = []
    for table in profile.tables:
        tname = table.table_name
        if table.duplicate_row_count:
            duplicate_findings.append(DuplicateFinding(
                table=tname, keys=["*all columns*"],
                duplicate_group_count=table.duplicate_row_count,
                affected_row_count=table.duplicate_row_count,
                likely_legitimate=False,
                assessment="Rows identical in every column. Usually a double export, but a "
                           "genuinely repeated transaction is possible, so removal is "
                           "proposed rather than applied.",
            ))
            add(
                TransformOp.DEDUPLICATE, tname, {"subset": None, "keep": "first"},
                title=f"Remove {table.duplicate_row_count} fully duplicated row(s)",
                reason="These rows are identical in every column and would double-count in "
                       "every additive measure.",
                effect=f"Would remove {table.duplicate_row_count} of {table.row_count} rows. "
                       f"Requires approval.",
                evidence=[f"{table.duplicate_row_count} exact duplicate row(s) measured"],
                risk=Risk.DESTRUCTIVE,
            )
        for col in table.columns:
            if any(i.code == "column.duplicate_key_values" for i in col.issues):
                m = next((x for x in mappings
                          if x.source_table == tname and x.source_column == col.name), None)
                duplicate_findings.append(DuplicateFinding(
                    table=tname, keys=[m.canonical_name if m else col.name],
                    duplicate_group_count=col.duplicate_count,
                    affected_row_count=col.duplicate_count * 2,
                    likely_legitimate=False,
                    assessment="A near-unique key repeats. If the repeated rows differ in "
                               "their other columns this is a conflicting record that must "
                               "be adjudicated by the data owner -- it is reported, never "
                               "auto-resolved.",
                ))

    # ------------------------------------------------------------------
    # 3. Append tables that describe the same entity
    # ------------------------------------------------------------------
    groups = _append_groups(profile, mappings)
    appended: dict[str, list[str]] = {}
    for group in groups:
        name = _dataset_name(group)
        appended[name] = group
        total = sum(t.row_count for t in profile.tables if t.table_name in group)
        add(
            TransformOp.APPEND_TABLES, name, {"tables": group, "source_column": "source_table"},
            title=f"Append {len(group)} table(s) into '{name}'",
            reason="These tables describe the same entity with different column names; "
                   "after mapping they share a schema, so they belong in one table rather "
                   "than being related to each other.",
            effect=f"Produces '{name}' with {total:,} rows and a 'source_table' column "
                   f"recording each row's origin.",
            evidence=[f"{t}: {next(x.row_count for x in profile.tables if x.table_name == t):,}"
                      f" rows" for t in group],
        )

    # ------------------------------------------------------------------
    # 4. Join proposals -- from measured overlap only
    # ------------------------------------------------------------------
    joins: list[JoinProposal] = []
    members = {t for group in appended.values() for t in group}
    for overlap in profile.key_overlaps:
        oriented = orient(overlap)
        if oriented is None:
            continue
        child_t, child_c, parent_t, parent_c = oriented
        if child_t in members and parent_t in members:
            continue  # same appended entity; not a join
        child_share = (
            overlap.left_match_share if f"{child_t}.{child_c}" == overlap.left
            else overlap.right_match_share
        )
        if child_share < MIN_CANDIDATE_SHARE:
            continue
        child_distinct = (
            overlap.left_distinct if f"{child_t}.{child_c}" == overlap.left
            else overlap.right_distinct
        )
        parent_unique = (
            overlap.right_is_unique if f"{parent_t}.{parent_c}" == overlap.right
            else overlap.left_is_unique
        )
        confidence = (
            Confidence.HIGH if child_share >= STRONG_SHARE and parent_unique
            else Confidence.MEDIUM if child_share >= 0.8
            else Confidence.LOW
        )
        canon = {
            (m.source_table, m.source_column): m.canonical_name for m in mappings
        }
        joins.append(JoinProposal(
            left_table=child_t, left_keys=[canon.get((child_t, child_c), child_c)],
            right_table=parent_t, right_keys=[canon.get((parent_t, parent_c), parent_c)],
            kind="left",
            implied_cardinality=overlap.implied_cardinality,
            match_share=round(child_share, 4),
            unmatched_left=round(child_distinct * (1 - child_share)),
            unmatched_right=max(0, overlap.right_distinct - overlap.matched_distinct),
            fan_out_risk=not parent_unique,
            recommended=parent_unique and child_share >= MIN_CANDIDATE_SHARE,
            recommendation_reason=(
                f"{child_share:.1%} of {child_t}.{child_c} keys resolve in "
                f"{parent_t}.{parent_c}"
                + ("" if parent_unique else
                   "; the parent side is NOT unique, so this would fan out and inflate "
                   "additive measures")
            ),
            confidence=confidence,
            evidence=[
                f"{overlap.matched_distinct} of {child_distinct} distinct child keys matched",
                f"parent side unique: {parent_unique}",
                f"name similarity {overlap.name_similarity:.2f} (context only, not the basis "
                f"for this proposal)",
            ] + ([f"unmatched examples: "
                  f"{', '.join((overlap.unmatched_left_examples if f'{child_t}.{child_c}' == overlap.left else overlap.unmatched_right_examples)[:4])}"]
                 if child_share < 1.0 else []),
        ))

    # Unknown members for orphan keys, so no row disappears from a total.
    for join in joins:
        if join.unmatched_left and join.recommended:
            add(
                TransformOp.BUILD_DIMENSION, join.right_table,
                {"add_unknown_member": True, "key": join.right_keys[0],
                 "unknown_key": "UNASSIGNED",
                 "source_keys_from": f"{join.left_table}.{join.left_keys[0]}"},
                title=f"Add an 'UNASSIGNED' member to {join.right_table} for "
                      f"{join.unmatched_left} unmatched key(s)",
                reason="Keys present in the fact table but absent from the dimension would "
                       "otherwise drop those rows out of every sliced total, so the report "
                       "would not reconcile to the source.",
                effect=f"Adds one 'UNASSIGNED' dimension row; {join.unmatched_left} orphan "
                       f"key(s) remain visible and are attributed to it.",
                evidence=join.evidence[:2],
                columns=[join.right_keys[0]],
            )

    # ------------------------------------------------------------------
    # 5. A date table, if any date survived and time intelligence is wanted
    # ------------------------------------------------------------------
    date_columns: list[str] = []
    for table in profile.tables:
        for col in table.columns:
            if col.inferred_type.is_temporal:
                m = next((x for x in mappings if x.source_table == table.table_name
                          and x.source_column == col.name), None)
                date_columns.append(
                    f"{table.table_name}.{m.canonical_name if m else col.name}"
                )
    wants_time = bool(
        spec and any(k.time_intelligence for k in spec.kpis)
    ) or bool(spec and spec.expected_grain.value in {"monthly", "quarterly", "yearly", "daily"})
    if date_columns and (wants_time or len(date_columns) > 1):
        bounds = _date_bounds(profile)
        add(
            TransformOp.BUILD_DATE_TABLE, "dim_date",
            {"start": bounds[0], "end": bounds[1], "fiscal_year_start_month": 1,
             "mark_as_date_table": True, "covers": date_columns},
            title="Build a contiguous date dimension",
            reason="Time intelligence (year-to-date, month-over-month) requires a marked "
                   "date table with no gaps. Several fact tables also carry dates, so they "
                   "need one shared calendar to be comparable.",
            effect=f"Creates dim_date covering {bounds[0]} to {bounds[1]} with year, "
                   f"quarter, month, month name and a sort key.",
            evidence=[f"date columns found: {', '.join(date_columns[:5])}"],
        )

    # ------------------------------------------------------------------
    # 6. Conformed dimensions for shared categories
    # ------------------------------------------------------------------
    for issue in profile.cross_table_issues:
        if issue.code != "keys.shared_category":
            continue
        left, right = issue.location.split(" <-> ")
        lt, lc = left.split(".", 1)
        rt, rc = right.split(".", 1)
        canon = {(m.source_table, m.source_column): m.canonical_name for m in mappings}
        column = canon.get((lt, lc), lc)
        dim = f"dim_{column}"
        if any(s.target_table == dim for s in steps):
            continue
        # The rename step runs first, so the dimension must read the CANONICAL
        # column names -- referencing the original headers would find nothing.
        sources = [
            f"{lt}.{canon.get((lt, lc), lc)}",
            f"{rt}.{canon.get((rt, rc), rc)}",
        ]
        add(
            TransformOp.BUILD_DIMENSION, dim,
            {"from_columns": sources, "key": column, "distinct": True},
            title=f"Build a conformed '{column}' dimension",
            reason="The same attribute is recorded in more than one table. One shared "
                   "dimension means a slicer filters every fact table consistently instead "
                   "of only one of them.",
            effect=f"Creates {dim} from the distinct values of both columns.",
            evidence=[issue.message],
        )

    # ------------------------------------------------------------------
    # 7. Conflicting vocabularies -> explicit mapping, needs approval
    # ------------------------------------------------------------------
    for issue in profile.cross_table_issues:
        if issue.code != "keys.conflicting_category_values":
            continue
        add(
            TransformOp.MAP_CATEGORY, issue.location.split(" vs ")[0].split(".")[0],
            {"column": issue.location.split(".")[-1].split(" vs ")[0],
             "mapping": {}, "source_of_truth": ""},
            title=f"Reconcile conflicting '{issue.location}' vocabularies",
            reason=issue.message,
            effect="No mapping is applied until the correspondence between the two value "
                   "sets is confirmed; the columns stay separate in the meantime.",
            evidence=[str(issue.evidence)[:200]],
            confidence=Confidence.LOW,
        )

    outputs = sorted({
        *(appended.keys()),
        *(t.table_name for t in profile.tables
          if t.table_name not in {x for g in appended.values() for x in g}),
        *(s.target_table for s in steps if s.op in {
            TransformOp.BUILD_DATE_TABLE, TransformOp.BUILD_DIMENSION
        }),
    })

    plan = TransformationPlan(
        project_id=project_id, mappings=mappings, steps=steps,
        join_proposals=joins, duplicate_findings=duplicate_findings,
        output_datasets=outputs,
        summary=(
            f"{len(mappings)} column(s) mapped to canonical names; {len(steps)} "
            f"transformation step(s) planned across {len(outputs)} output dataset(s); "
            f"{len(joins)} relationship(s) proposed from measured key overlap. "
            f"{sum(1 for s in steps if s.requires_approval)} step(s) need approval."
        ),
        provenance=Provenance(
            origin=Origin.DETERMINISTIC, produced_by="data_preparation_agent",
            confidence=Confidence.HIGH,
            rationale="Every step is derived from a measured property of the profiled data. "
                      "Joins come from value-overlap measurement, not column-name similarity. "
                      "Steps that could drop or merge rows are marked destructive and gated "
                      "on approval.",
            evidence=[
                f"{len(profile.tables)} source table(s) profiled",
                f"{len(profile.key_overlaps)} key pair(s) measured",
                f"{len(duplicate_findings)} duplicate finding(s) reported without removal",
            ],
        ),
    )
    return plan


def _date_bounds(profile: SourceDataProfile) -> tuple[str, str]:
    """Span every real date found, widened to whole years."""
    lo, hi = None, None
    for table in profile.tables:
        for col in table.columns:
            if col.temporal and col.temporal.min_date and col.temporal.max_date:
                lo = col.temporal.min_date if lo is None else min(lo, col.temporal.min_date)
                hi = col.temporal.max_date if hi is None else max(hi, col.temporal.max_date)
    if lo is None or hi is None:
        from datetime import date
        y = date.today().year
        return f"{y}-01-01", f"{y}-12-31"
    return f"{lo[:4]}-01-01", f"{hi[:4]}-12-31"
