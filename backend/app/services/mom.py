"""Rendering the Minutes of Meeting to Markdown.

The MoM is a deliverable in its own right: stakeholders read and sign off on
it. So the rendering is explicit about provenance -- it states which model (or
that the heuristic extractor) produced it, and it never presents an inferred
attendee or an unassigned action item as if it were settled.
"""
from __future__ import annotations

from ..schemas.requirements import MeetingMinutes


def _bullets(items: list[str], empty: str = "_None recorded._") -> str:
    return "\n".join(f"- {i}" for i in items) if items else empty


def render_minutes(m: MeetingMinutes) -> str:
    lines: list[str] = []
    add = lines.append

    add(f"# {m.title}")
    add("")
    meta = []
    if m.meeting_date:
        meta.append(f"**Date:** {m.meeting_date}")
    if m.duration_hint:
        meta.append(f"**Duration:** {m.duration_hint}")
    meta.append(f"**Transcript length:** {m.transcript_line_count} lines")
    add(" &nbsp;·&nbsp; ".join(meta))
    add("")

    origin = m.provenance.origin.value
    how = {
        "llm": "extracted by a language model from the transcript",
        "heuristic": "extracted by the deterministic rule-based extractor "
                     "(no language model is configured in this deployment)",
        "user": "supplied by a user",
        "deterministic": "computed directly from the transcript",
    }.get(origin, origin)
    add(f"> These minutes were {how}. Every statement below is traceable to the "
        f"transcript lines cited beside it. Please correct anything mis-recorded "
        f"before the requirements are approved.")
    add("")

    add("## Attendees")
    if m.attendees:
        add("")
        add("| Name | Role | Speaking turns | Source |")
        add("|---|---|---|---|")
        for a in m.attendees:
            src = "inferred from speaker labels" if a.inferred else "stated"
            role = a.role or "—"
            if a.organisation:
                role = f"{role}, {a.organisation}"
            add(f"| {a.name} | {role} | {a.speaking_turns} | {src} |")
    else:
        add("")
        add("_No attendees could be identified from the transcript._")
    add("")

    if m.agenda:
        add("## Agenda")
        add("")
        add(_bullets(m.agenda))
        add("")

    if m.executive_summary:
        add("## Summary")
        add("")
        add(m.executive_summary)
        add("")

    add("## Discussion")
    add("")
    if m.discussion_points:
        for d in m.discussion_points:
            add(f"### {d.topic}")
            add("")
            add(d.summary)
            if d.participants:
                add("")
                add(f"_Raised by: {', '.join(d.participants)}_")
            for sp in d.spans[:2]:
                if sp.quote:
                    speaker = f"**{sp.speaker}:** " if sp.speaker else ""
                    add("")
                    add(f"> {speaker}{sp.quote}  ")
                    add(f"> _(lines {sp.start_line}–{sp.end_line})_")
            add("")
    else:
        add("_No distinct discussion topics were identified._")
        add("")

    add("## Decisions")
    add("")
    if m.decisions:
        for i, d in enumerate(m.decisions, 1):
            who = f" — decided by {d.decided_by}" if d.decided_by else ""
            cite = (f" _(lines {d.spans[0].start_line}–{d.spans[0].end_line})_"
                    if d.spans else "")
            add(f"{i}. **{d.statement}**{who}{cite}")
            if d.affects:
                add(f"   - Affects: {', '.join(d.affects)}")
            if d.confidence.value != "high":
                add(f"   - ⚠️ Recorded with {d.confidence.value} confidence — please confirm.")
        add("")
    else:
        add("_No explicit decisions were reached. Anything that sounded like a "
            "conclusion but was not clearly agreed is listed under Open questions "
            "instead of being recorded as a decision._")
        add("")

    add("## Action items")
    add("")
    if m.action_items:
        add("| # | Action | Owner | Due | Status |")
        add("|---|---|---|---|---|")
        for i, a in enumerate(m.action_items, 1):
            owner = a.owner or "**unassigned**"
            add(f"| {i} | {a.description} | {owner} | {a.due or '—'} | {a.status} |")
        unassigned = m.unassigned_action_items
        if unassigned:
            add("")
            add(f"⚠️ {len(unassigned)} action item(s) have no owner. "
                f"They will not progress until someone is named.")
        add("")
    else:
        add("_No action items were recorded._")
        add("")

    add("## Open questions")
    add("")
    if m.open_questions:
        for q in m.open_questions:
            status = f"✅ {q.answer}" if q.answered else f"❓ unanswered ({q.severity.value})"
            add(f"- **{q.question}**")
            if q.why_it_matters:
                add(f"  - Why it matters: {q.why_it_matters}")
            if q.blocks:
                add(f"  - Blocks: {', '.join(q.blocks)}")
            add(f"  - Status: {status}")
        add("")
    else:
        add("_Nothing outstanding._")
        add("")

    if m.risks:
        add("## Risks raised")
        add("")
        add(_bullets(m.risks))
        add("")

    add("---")
    add("")
    add(f"_Generated {m.generated_at.strftime('%Y-%m-%d %H:%M UTC')} by "
        f"{m.provenance.produced_by} (origin: {origin})._")
    return "\n".join(lines)
