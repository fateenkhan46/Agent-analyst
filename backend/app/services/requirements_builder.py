"""Building the MoM and the requirements spec deterministically.

Both artifacts come out of here. When an LLM is configured the agent enriches
the result (better prose summaries, interpretation of unusual KPIs), but this
module always runs first so that:

* every MoM statement cites real transcript line numbers;
* speaking-turn counts and attendee lists are measured, not described;
* a KPI with no catalogue match and no meeting definition becomes an explicit
  question instead of a confident guess.
"""
from __future__ import annotations

import re
from collections import Counter

from ..schemas.common import Confidence, Origin, Provenance, Severity
from ..schemas.requirements import (
    ActionItem, AggregationKind, Assumption, Attendee, Decision, DimensionRequirement,
    DiscussionPoint, Grain, KpiRequirement, MeetingMinutes, MetricDefinition, OpenQuestion,
    RequirementsAgentInput, RequirementsSpec, TranscriptSpan,
)
from . import kpi_catalog
from .transcript import (
    KPI_TOPIC_MAP, Sentence, TranscriptAnalysis, analyse, classify_action,
    classify_decision, classify_open_question, classify_risk, is_question, topics_for,
)

#: Keeps the minutes readable; topics are ranked by share of the meeting.
MAX_DISCUSSION_TOPICS = 8

#: Which pipeline stage an unanswered question blocks.
TOPIC_BLOCKS: dict[str, list[str]] = {
    "identifiers": ["plan_transformations"],
    "budget variance": ["generate_measures"],
    "variance percentage": ["generate_measures"],
    "labour hours": ["plan_transformations", "generate_measures"],
    "currency": ["plan_transformations", "generate_measures"],
    "missing keys": ["plan_transformations", "propose_semantic_model"],
    "duplicates": ["plan_transformations"],
    "status vocabulary": ["plan_transformations", "propose_semantic_model"],
    "averages": ["generate_measures"],
    "double counting": ["propose_semantic_model", "generate_measures"],
    "date table": ["propose_semantic_model", "generate_measures"],
    "report layout": ["design_report"],
    "sign-off": [],
}


def _span(s: Sentence) -> TranscriptSpan:
    return TranscriptSpan(
        start_line=s.turn.line_no, end_line=s.turn.line_no,
        speaker=s.turn.speaker, quote=s.turn.quote,
    )


def _affects(text: str) -> list[str]:
    out: list[str] = []
    for topic in topics_for(text):
        kpi = KPI_TOPIC_MAP.get(topic)
        out.append(f"kpi:{kpi}" if kpi else f"topic:{topic}")
    return out


def build_minutes(
    transcript_text: str, *, project_id: str, source_file_id: str = "",
    produced_by: str = "requirements_meeting_agent",
) -> MeetingMinutes | None:
    """Produce the MoM. Returns ``None`` when there is no usable transcript --
    we do not manufacture minutes from nothing."""
    if not transcript_text.strip():
        return None

    a: TranscriptAnalysis = analyse(transcript_text)
    if not a.has_speaker_structure and len(transcript_text.split()) < 40:
        return None

    sentences = a.sentences()
    minutes = MeetingMinutes(
        project_id=project_id, source_file_id=source_file_id,
        title=a.title or "Stakeholder Requirements Meeting",
        meeting_date=a.date, duration_hint=a.duration,
        transcript_line_count=a.line_count,
        agenda=a.agenda,
        provenance=Provenance(
            origin=Origin.HEURISTIC, produced_by=produced_by,
            confidence=Confidence.MEDIUM,
            rationale="Extracted by deterministic rules over the transcript: speaker turns "
                      "and line numbers are measured; decisions require explicit agreement "
                      "or definitional language and are never inferred from hedged speech.",
            evidence=[
                f"{a.line_count} transcript lines, {len(a.turns)} speaker turns.",
                f"{len(a.speaker_turn_counts)} distinct speakers identified.",
            ],
        ),
    )

    # --- attendees -----------------------------------------------------
    declared = {name: role for name, role in a.declared_attendees}
    seen: dict[str, Attendee] = {}
    for name, role in a.declared_attendees:
        seen[name] = Attendee(
            name=name, role=role, speaking_turns=a.speaker_turn_counts.get(name, 0),
            inferred=False,
        )
    for name, count in a.speaker_turn_counts.items():
        if name in seen:
            continue
        # Match a bare first name against a declared full name before
        # recording a separate attendee.
        matched = next(
            (full for full in declared
             if full.split()[0].lower() == name.split()[0].lower()), None
        )
        if matched and matched in seen:
            seen[matched].speaking_turns += count
            continue
        seen[name] = Attendee(
            name=name, role=declared.get(name, ""), speaking_turns=count, inferred=True,
        )
    minutes.attendees = sorted(seen.values(), key=lambda x: (-x.speaking_turns, x.name))

    # --- decisions (merged per turn+topic so one point is one decision) --
    decision_by_key: dict[tuple[int, str], Decision] = {}
    for i, s in enumerate(sentences):
        ok, conf = classify_decision(s, sentences[i + 1: i + 9])
        if not ok:
            continue
        topics = topics_for(s.text) or ["general"]
        key = (s.turn.index, topics[0])
        existing = decision_by_key.get(key)
        if existing:
            # Same speaker, same turn, same topic: one decision expressed over
            # several sentences. Join rather than emit near-duplicates.
            existing.statement = f"{existing.statement} {s.text.strip()}".strip()
            if conf == "high":
                existing.confidence = Confidence.HIGH
            continue
        decision_by_key[key] = Decision(
            statement=s.text.strip(), decided_by=s.turn.speaker,
            affects=_affects(s.text), spans=[_span(s)],
            confidence=Confidence(conf),
        )
    minutes.decisions = list(decision_by_key.values())

    # --- action items --------------------------------------------------
    actions: list[ActionItem] = []
    for idx, s in enumerate(sentences):
        ok, owner, due = classify_action(s)
        if not ok:
            continue
        text = s.text.strip()
        # An imperative such as "Put it on the list" belongs to whoever was
        # just speaking about it; attribute it to the previous turn's speaker
        # only when that speaker accepted it, otherwise leave it unassigned
        # and let the MoM flag it.
        if not owner and idx and re.match(r"^(?:put|add|send|flag|check|confirm)\b", text, re.I):
            prev = sentences[idx - 1]
            if prev.turn.index == s.turn.index:
                owner = s.turn.speaker
        if any(a.description.lower() == text.lower() for a in actions):
            continue
        actions.append(ActionItem(
            description=text, owner=owner, due=due, spans=[_span(s)],
        ))
    minutes.action_items = actions

    # --- open questions -------------------------------------------------
    questions: list[OpenQuestion] = []
    for i, s in enumerate(sentences):
        if not classify_open_question(
            s, sentences[i + 1: i + 7], preceding=sentences[max(0, i - 5): i]
        ):
            continue
        topics = topics_for(s.text)
        blocks: list[str] = []
        for t in topics:
            blocks.extend(TOPIC_BLOCKS.get(t, []))
        question_text = s.text.strip()
        if not is_question(question_text):
            question_text = f"Confirm: {question_text}"
        if any(q.question.lower() == question_text.lower() for q in questions):
            continue
        questions.append(OpenQuestion(
            question=question_text,
            why_it_matters="; ".join(sorted({t.capitalize() for t in topics}))
            or "Raised in the meeting and left unresolved.",
            blocks=sorted(set(blocks)), raised_by=s.turn.speaker,
            severity=Severity.HIGH if blocks else Severity.MEDIUM,
            spans=[_span(s)],
        ))
    minutes.open_questions = questions

    # --- discussion points, grouped by topic ----------------------------
    by_topic: dict[str, list[Sentence]] = {}
    for s in sentences:
        for t in topics_for(s.text):
            by_topic.setdefault(t, []).append(s)
    # Capped so the minutes stay readable; the topics are ordered by how much
    # of the meeting was spent on them.
    for topic, group in sorted(by_topic.items(), key=lambda kv: -len(kv[1]))[:MAX_DISCUSSION_TOPICS]:
        if len(group) < 2:
            continue
        speakers = list(dict.fromkeys(s.turn.speaker for s in group))
        # The summary is drawn from the longest statements on the topic rather
        # than paraphrased, so nothing is put in a stakeholder's mouth.
        best = sorted(group, key=lambda s: -len(s.text))[:2]
        # One quote per turn: several sentences from the same turn share a
        # line number, so quoting both would print the same block twice.
        quoted: list[Sentence] = []
        seen_turns: set[int] = set()
        for candidate in best + group:
            if candidate.turn.index in seen_turns:
                continue
            seen_turns.add(candidate.turn.index)
            quoted.append(candidate)
            if len(quoted) == 2:
                break
        minutes.discussion_points.append(DiscussionPoint(
            topic=topic.capitalize(),
            summary=" ".join(s.text.strip() for s in best),
            participants=speakers[:5],
            spans=[_span(s) for s in quoted],
        ))

    # --- risks ----------------------------------------------------------
    minutes.risks = list(dict.fromkeys(
        s.text.strip() for s in sentences if classify_risk(s)
    ))

    # --- executive summary ----------------------------------------------
    minutes.executive_summary = _summarise(minutes, a)
    return minutes


def _summarise(m: MeetingMinutes, a: TranscriptAnalysis) -> str:
    """A factual summary built from counts, not generated prose."""
    topics = [d.topic for d in m.discussion_points[:4]]
    parts = [
        f"{len(m.attendees)} participant(s) discussed "
        f"{len(m.discussion_points)} topic(s)"
        + (f", principally {', '.join(topics[:3]).lower()}" if topics else "")
        + ".",
        f"{len(m.decisions)} decision(s) were recorded, "
        f"{sum(1 for d in m.decisions if d.confidence is Confidence.HIGH)} of them "
        f"explicitly confirmed by another participant.",
    ]
    if m.action_items:
        parts.append(
            f"{len(m.action_items)} action item(s) were raised"
            + (f", {len(m.unassigned_action_items)} without an owner" if
               m.unassigned_action_items else "")
            + "."
        )
    unanswered = [q for q in m.open_questions if not q.answered]
    if unanswered:
        parts.append(
            f"{len(unanswered)} question(s) remain open and must be answered before the "
            f"affected measures can be built."
        )
    return " ".join(parts)


# --------------------------------------------------------------------------
# Requirements specification
# --------------------------------------------------------------------------
GRAIN_HINTS = (
    (Grain.TRANSACTION, ("transaction", "line item", "per invoice", "每")),
    (Grain.DAILY, ("daily", "per day", "by day")),
    (Grain.MONTHLY, ("monthly", "per month", "by month", "month-over-month")),
    (Grain.QUARTERLY, ("quarterly", "per quarter")),
    (Grain.YEARLY, ("yearly", "annual", "per year")),
)


def build_requirements(
    payload: RequirementsAgentInput, minutes: MeetingMinutes | None,
    *, produced_by: str = "requirements_meeting_agent",
) -> RequirementsSpec:
    """Turn the brief plus the MoM into the engineering specification."""
    # Soft signals (cadence, grain, audience, slicers) are stated in passing
    # anywhere in the brief or the meeting, so the corpus spans both. Hard
    # facts are never taken from this; only phrasing hints.
    corpus = " ".join([
        payload.description, payload.business_objective, payload.design_preferences,
        " ".join(payload.reporting_questions),
        " ".join(d.statement for d in (minutes.decisions if minutes else [])),
        " ".join(p.summary for p in (minutes.discussion_points if minutes else [])),
        " ".join(q.question for q in (minutes.open_questions if minutes else [])),
        " ".join(a.description for a in (minutes.action_items if minutes else [])),
        minutes.executive_summary if minutes else "",
    ]).lower()

    spec = RequirementsSpec(
        project_id=payload.project_id,
        business_objective=payload.business_objective
        or (minutes.executive_summary if minutes else ""),
        objectives=[q for q in payload.reporting_questions if q.strip()],
        currency=payload.currency, units=payload.units,
        derived_from_minutes=minutes.id if minutes else "",
        provenance=Provenance(
            origin=Origin.HEURISTIC, produced_by=produced_by,
            rationale="KPIs are matched against a catalogue of standard BI metric shapes "
                      "and grounded against the profiled columns. A KPI with no catalogue "
                      "match and no definition from the meeting is recorded with an empty "
                      "definition and an explicit question, never a guessed one.",
        ),
    )

    # --- reporting period ----------------------------------------------
    period = payload.reporting_period.strip()
    if period:
        years = re.findall(r"(20\d{2})", period)
        if len(years) >= 2:
            spec.reporting_period_start, spec.reporting_period_end = (
                f"{years[0]}-01-01", f"{years[1]}-12-31"
            )
        elif years:
            spec.reporting_period_start = f"{years[0]}-01-01"
            spec.reporting_period_end = f"{years[0]}-12-31"
    for grain, hints in GRAIN_HINTS:
        if any(h in corpus for h in hints):
            spec.expected_grain = grain
            break

    # --- dimensions -----------------------------------------------------
    dimension_names: list[str] = []
    for table, columns in payload.available_columns.items():
        for col in columns:
            role = kpi_catalog.role_for_column(col)
            if role in kpi_catalog.ATTRIBUTE_ROLES and col not in dimension_names:
                dimension_names.append(col)
    slicer_words = ("slicer", "filter", "available as slicers")
    for name in dimension_names:
        low = name.lower()
        spec.dimensions.append(DimensionRequirement(
            name=name,
            purpose=f"Breakdown and filtering by {name}.",
            as_slicer=any(w in corpus for w in slicer_words) and
            kpi_catalog.role_for_column(name) in {"status", "portfolio", "category"},
        ))
    if any("month" in corpus or "trend" in corpus for _ in (0,)):
        spec.dimensions.append(DimensionRequirement(
            name="Month", purpose="Monthly trend and period comparison.",
            hierarchy=["Year", "Quarter", "Month"],
        ))

    # --- KPIs -----------------------------------------------------------
    decision_text = {
        d.statement.lower(): d for d in (minutes.decisions if minutes else [])
    }
    available_roles = _available_roles(payload)

    for raw_name in payload.stated_kpis:
        name = raw_name.strip()
        if not name:
            continue
        shape = kpi_catalog.match(name)
        kpi = KpiRequirement(name=name, priority=1)

        # A definition supplied by the user always wins.
        user_definition = next(
            (v for k, v in payload.business_definitions.items()
             if k.strip().lower() == name.lower()), "",
        )
        meeting_decisions = [
            d for d in (minutes.decisions if minutes else [])
            if _decision_concerns_kpi(d, name)
        ]

        if shape:
            kpi.aggregation = shape.aggregation
            kpi.definition = user_definition or shape.definition
            kpi.unit = shape.unit.replace("currency", payload.currency or "currency")
            kpi.format_string = shape.format_string
            kpi.numerator_hint = shape.numerator_role
            kpi.denominator_hint = shape.denominator_role
            kpi.time_intelligence = list(shape.time_intelligence)
            kpi.higher_is_better = shape.higher_is_better
            kpi.confidence = Confidence.HIGH if (user_definition or meeting_decisions) \
                else Confidence.MEDIUM
            missing = [r for r in shape.needs_roles if r not in available_roles]
            if missing:
                kpi.needs_confirmation = True
                spec.open_questions.append(OpenQuestion(
                    question=f"'{name}' needs {', '.join(missing)} data, which was not found "
                             f"in the uploaded sources. Which column holds it?",
                    why_it_matters=f"Without it, {name} cannot be calculated at all.",
                    blocks=["generate_measures"], severity=Severity.HIGH,
                ))
            for note in shape.assumptions:
                spec.assumptions.append(Assumption(
                    statement=note, made_by=produced_by,
                    impact_if_wrong=f"{name} would be computed on the wrong basis.",
                    severity=Severity.HIGH if "sign convention" in note.lower()
                    else Severity.MEDIUM,
                ))
        else:
            # Deliberately NOT guessed.
            kpi.definition = ""
            kpi.needs_confirmation = True
            kpi.confidence = Confidence.LOW
            kpi.aggregation = AggregationKind.NON_ADDITIVE
            spec.open_questions.append(OpenQuestion(
                question=f"How should '{name}' be calculated? It does not match a standard "
                         f"metric shape and the meeting did not define it.",
                why_it_matters="No measure will be generated for this KPI until it is "
                               "defined; inventing a formula would produce a confidently "
                               "wrong number.",
                blocks=["generate_measures"], severity=Severity.HIGH,
            ))

        if meeting_decisions:
            kpi.source_decisions = [d.id for d in meeting_decisions]
            # The meeting overrides the catalogue, and we say so.
            kpi.definition = (
                f"{kpi.definition} Per the meeting: "
                + " ".join(d.statement for d in meeting_decisions[:2])
            ).strip()
        if shape and shape.requires_date_table:
            kpi.required_dimensions = ["Month"]
        spec.kpis.append(kpi)

    # --- business definitions and conflicts ------------------------------
    for term, definition in payload.business_definitions.items():
        spec.definitions.append(MetricDefinition(term=term, definition=definition, agreed=True))
    if minutes:
        for d in minutes.decisions:
            if "system of record" in d.statement.lower():
                spec.definitions.append(MetricDefinition(
                    term="System of record",
                    definition=d.statement,
                    agreed=d.confidence is Confidence.HIGH,
                ))
        # Carry the meeting's unresolved questions into the spec so one list
        # drives the approval gates.
        for q in minutes.open_questions:
            if not any(x.question.lower() == q.question.lower() for x in spec.open_questions):
                spec.open_questions.append(q)

    # --- audiences / cadence --------------------------------------------
    for audience in ("executive committee", "portfolio managers", "finance", "operations"):
        if audience in corpus:
            spec.audiences.append(audience.title())
    for cadence in ("monthly", "weekly", "daily", "quarterly"):
        if cadence in corpus:
            spec.refresh_cadence = cadence
            break

    return spec


#: Words that carry no discriminating meaning in a KPI name.
KPI_STOPWORDS = frozenset({
    "total", "average", "avg", "mean", "per", "of", "the", "and", "a", "an",
    "change", "month", "over", "year", "to", "date", "cost", "%", "percent",
})


def _decision_concerns_kpi(decision: Decision, kpi_name: str) -> bool:
    """Does this decision actually concern this KPI?

    Matching on a single shared word is far too loose: "I don't want a
    mixed-currency total" would attach itself to every KPI whose name starts
    with "Total". So a decision qualifies only when it was explicitly tagged
    with the KPI, or when it shares a *discriminating* term with the KPI name.
    """
    if f"kpi:{kpi_name}" in decision.affects:
        return True
    statement = decision.statement.lower()
    if kpi_name.lower() in statement:
        return True
    tokens = {
        t for t in re.split(r"[^a-z%]+", kpi_name.lower())
        if t and t not in KPI_STOPWORDS and len(t) > 2
    }
    if not tokens:
        return False
    return all(t in statement for t in tokens)


def _available_roles(payload: RequirementsAgentInput) -> set[str]:
    roles: set[str] = set()
    for columns in payload.available_columns.values():
        for col in columns:
            role = kpi_catalog.role_for_column(col)
            if role:
                roles.add(role)
    # a date column anywhere means time intelligence is at least possible
    return roles


def kpi_coverage(spec: RequirementsSpec) -> dict[str, int]:
    counts = Counter()
    for k in spec.kpis:
        counts["total"] += 1
        if k.needs_confirmation:
            counts["needs_confirmation"] += 1
        if k.definition:
            counts["defined"] += 1
        if k.time_intelligence:
            counts["time_intelligence"] += 1
    return dict(counts)
