"""Requirements and Meeting Agent -- Step 3.

Produces **two** artifacts from the brief and the stakeholder transcript:

1. ``MeetingMinutes`` (the MoM) -- attendees, agenda, discussion, decisions,
   action items, open questions and risks, each citing transcript line numbers.
2. ``RequirementsSpec`` -- the engineering specification every later agent
   reads its goals from.

The deterministic extractor always runs first, because speaker turns, counts
and line numbers must be measured rather than described. When a language model
is configured it is then asked to improve the *interpretive* parts -- the
executive summary, discussion topic naming, and the definition of any KPI that
matched no catalogue shape -- and is explicitly forbidden from inventing a
definition: if it cannot ground one, the KPI keeps its open question.
"""
from __future__ import annotations

from pydantic import Field

from ..llm.provider import NoHeuristicAvailable, StructuredRequest, ask
from ..schemas.agent import AgentName, AgentPolicy, AgentSpec
from ..schemas.common import Confidence, Contract, Origin, Severity
from ..schemas.requirements import (
    OpenQuestion, RequirementsAgentInput, RequirementsAgentOutput,
)
from .base import Agent, AgentContext

SYSTEM = """You are a senior BI business analyst writing the minutes of a \
requirements meeting and an engineering specification.

Rules you must follow:
- Never invent a KPI definition. If the transcript and brief do not define a \
metric, say so and leave it undefined.
- Never attribute a statement to a person who did not make it.
- Prefer the stakeholders' own words over paraphrase for anything definitional.
- A hedged statement ("maybe", "I think") is not a decision."""


class EnrichedNarrative(Contract):
    """What the LLM is allowed to contribute. Deliberately narrow: prose and
    KPI interpretation only, never structure, counts or citations."""

    executive_summary: str = Field(
        default="", description="3-5 sentences. Factual; no recommendations."
    )
    topic_titles: dict[str, str] = Field(
        default_factory=dict,
        description="Map each supplied topic key to a clearer business title.",
    )
    kpi_definitions: dict[str, str] = Field(
        default_factory=dict,
        description="Only for KPIs listed as undefined, and only when the brief or "
        "transcript actually defines them. Omit any you cannot ground.",
    )
    additional_risks: list[str] = Field(default_factory=list, max_length=6)


class RequirementsMeetingAgent(Agent[RequirementsAgentInput, RequirementsAgentOutput]):
    input_model = RequirementsAgentInput
    output_model = RequirementsAgentOutput
    spec = AgentSpec(
        name=AgentName.REQUIREMENTS,
        purpose="Interpret the reporting brief and the stakeholder meeting transcript into "
                "minutes of meeting and a structured requirements specification, raising "
                "targeted questions instead of inventing business definitions.",
        input_schema="app.schemas.requirements.RequirementsAgentInput",
        output_schema="app.schemas.requirements.RequirementsAgentOutput",
        allowed_tools=["load_artifact", "list_source_files", "record_audit"],
        uses_llm=True,
        policy=AgentPolicy(
            timeout_seconds=120, max_retries=1, max_usd=0.35, max_llm_calls=2,
            on_exhausted="degrade_to_heuristic",
        ),
    )

    def run(
        self, payload: RequirementsAgentInput, ctx: AgentContext
    ) -> RequirementsAgentOutput:
        from ..services.requirements_builder import build_minutes, build_requirements

        # --- always: deterministic structure -------------------------------
        minutes = build_minutes(
            payload.transcript_text, project_id=payload.project_id,
            source_file_id=payload.transcript_source_file_id,
            produced_by=self.spec.name.value,
        )
        spec = build_requirements(payload, minutes, produced_by=self.spec.name.value)

        notes: list[str] = []
        if payload.transcript_text.strip() and minutes is None:
            notes.append(
                "A transcript was supplied but no speaker turns of the form 'Name: text' "
                "were found, so no minutes could be produced. The requirements below come "
                "from the brief alone."
            )
        elif minutes:
            ctx.note(
                f"Minutes: {len(minutes.attendees)} attendee(s), "
                f"{len(minutes.decisions)} decision(s), "
                f"{len(minutes.action_items)} action item(s), "
                f"{len(minutes.open_questions)} open question(s)."
            )
        elif not payload.transcript_text.strip():
            notes.append(
                "No meeting transcript was supplied, so no minutes were produced. "
                "Attach one to capture stakeholder decisions and action items."
            )

        undefined = [k.name for k in spec.kpis if not k.definition]
        ctx.note(
            f"{len(spec.kpis)} KPI(s): {len(spec.kpis) - len(undefined)} grounded, "
            f"{len(undefined)} undefined and queried."
        )

        # --- optionally: LLM enrichment of the interpretive parts ----------
        try:
            enriched = self._enrich(payload, spec, minutes, ctx)
        except NoHeuristicAvailable as exc:
            # Expected with no API key: the deterministic result stands.
            ctx.note(f"No LLM enrichment ({exc}). Deterministic extraction is authoritative.")
            enriched = None
        except Exception as exc:  # noqa: BLE001 - enrichment must never fail the task
            ctx.note(f"LLM enrichment failed ({type(exc).__name__}: {exc}); continuing "
                     f"with the deterministic extraction.")
            enriched = None

        if enriched is not None:
            if enriched.executive_summary and minutes:
                minutes.executive_summary = enriched.executive_summary
                minutes.provenance.origin = Origin.LLM
                minutes.provenance.rationale += (
                    " The narrative summary and topic titles were written by a language "
                    "model; attendees, counts, decisions and line citations remain the "
                    "deterministic extraction."
                )
            if minutes:
                for point in minutes.discussion_points:
                    better = enriched.topic_titles.get(point.topic) or \
                        enriched.topic_titles.get(point.topic.lower())
                    if better:
                        point.topic = better
                for risk in enriched.additional_risks:
                    if risk not in minutes.risks:
                        minutes.risks.append(risk)

            grounded = 0
            for kpi in spec.kpis:
                if kpi.definition:
                    continue
                definition = enriched.kpi_definitions.get(kpi.name)
                if not definition or not definition.strip():
                    continue
                kpi.definition = definition.strip()
                kpi.confidence = Confidence.LOW
                # Still unconfirmed: a model-supplied definition is a proposal.
                kpi.needs_confirmation = True
                grounded += 1
                spec.open_questions.append(OpenQuestion(
                    question=f"Is this the right definition of '{kpi.name}'? "
                             f"Proposed: {kpi.definition}",
                    why_it_matters="This definition was proposed by a language model from the "
                                   "brief, not taken from an agreed source. Confirm it before "
                                   "the measure is trusted.",
                    blocks=["generate_measures"], severity=Severity.HIGH,
                ))
            if grounded:
                ctx.note(f"{grounded} undefined KPI(s) received a model-proposed definition, "
                         f"each flagged for confirmation.")
                spec.provenance.origin = Origin.LLM

        # De-duplicate questions that the two paths may both have raised.
        seen: set[str] = set()
        unique: list[OpenQuestion] = []
        for q in spec.open_questions:
            key = q.question.strip().lower()
            if key not in seen:
                seen.add(key)
                unique.append(q)
        spec.open_questions = unique

        return RequirementsAgentOutput(spec=spec, minutes=minutes, notes=notes)

    # ------------------------------------------------------------------
    def _enrich(
        self, payload: RequirementsAgentInput, spec, minutes, ctx: AgentContext
    ) -> EnrichedNarrative | None:
        undefined = [k.name for k in spec.kpis if not k.definition]
        topics = [p.topic for p in (minutes.discussion_points if minutes else [])]
        if not undefined and not topics and not payload.transcript_text:
            return None

        prompt_parts = [
            f"# Project\n{payload.project_name}\n",
            f"## Business objective\n{payload.business_objective or '(not stated)'}\n",
        ]
        if payload.reporting_questions:
            prompt_parts.append(
                "## Reporting questions\n"
                + "\n".join(f"- {q}" for q in payload.reporting_questions) + "\n"
            )
        if payload.business_definitions:
            prompt_parts.append(
                "## Definitions supplied by the business\n"
                + "\n".join(f"- {k}: {v}" for k, v in payload.business_definitions.items())
                + "\n"
            )
        if minutes:
            prompt_parts.append(
                "## Decisions already extracted from the transcript (do not restate)\n"
                + "\n".join(f"- {d.statement}" for d in minutes.decisions[:20]) + "\n"
            )
            prompt_parts.append(f"## Topic keys to retitle\n{', '.join(topics)}\n")
        if payload.transcript_text:
            # Bounded so the prompt stays within budget on a long meeting.
            excerpt = payload.transcript_text[:14_000]
            prompt_parts.append(f"## Transcript excerpt\n{excerpt}\n")
        if undefined:
            prompt_parts.append(
                "## KPIs with no definition\n"
                + "\n".join(f"- {n}" for n in undefined)
                + "\n\nDefine one ONLY if the brief or transcript above actually says how "
                  "to calculate it. Omit the rest -- an omitted KPI becomes a question to "
                  "the user, which is the correct outcome.\n"
            )
        prompt_parts.append(
            f"## Available data\n"
            + "\n".join(
                f"- {t}: {', '.join(cols[:14])}"
                for t, cols in payload.available_columns.items()
            )
        )

        response = ask(ctx, StructuredRequest(
            purpose="requirements.narrative",
            system=SYSTEM,
            prompt="\n".join(prompt_parts),
            output_model=EnrichedNarrative,
            facts={"payload": payload.model_dump(mode="json")},
            max_output_tokens=2048,
        ))
        return response.value  # type: ignore[return-value]
