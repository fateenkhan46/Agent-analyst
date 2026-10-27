"""DAX Agent -- Step 6.

Generates measures from approved KPI definitions, then puts every one through
the parser, the model-aware reference validator and -- where the construct is
in the evaluable subset -- actual execution against the prepared data.

A measure that fails any gate is not returned as if it worked: it goes to
``rejected`` with the reason, and the orchestrator surfaces that on the DAX
Studio page.
"""
from __future__ import annotations

from pydantic import Field

from ..llm.provider import NoHeuristicAvailable, StructuredRequest, ask
from ..schemas.agent import AgentName, AgentPolicy, AgentSpec, AuditAction
from ..schemas.common import Contract, Origin, Severity
from ..schemas.requirements import RequirementsSpec
from ..schemas.semantic import (
    DaxAgentInput, DaxAgentOutput, MeasureDefinition, SemanticModel,
)
from .base import Agent, AgentContext

SYSTEM = """You are a senior DAX developer.

Rules:
- Only use tables, columns and measures that appear in the model given to you.
- Use DIVIDE for every division so a zero denominator returns BLANK.
- Never use a column without an aggregation in a measure body.
- Only use time intelligence if the model has a date table.
- If you cannot write a correct measure from the information given, say so \
rather than guessing: a wrong measure is worse than a missing one."""


class MeasureNarrative(Contract):
    """What an LLM may add to a generated measure: explanation only."""

    descriptions: dict[str, str] = Field(
        default_factory=dict, description="measure name -> plain-English explanation"
    )
    extra_assumptions: dict[str, list[str]] = Field(default_factory=dict)
    concerns: list[str] = Field(default_factory=list, max_length=8)


class DaxAgent(Agent[DaxAgentInput, DaxAgentOutput]):
    input_model = DaxAgentInput
    output_model = DaxAgentOutput
    spec = AgentSpec(
        name=AgentName.DAX,
        purpose="Convert approved KPI definitions into validated DAX measures, each with "
                "an explanation, stated assumptions, aggregation behaviour, dependencies "
                "and executable validation cases.",
        input_schema="app.schemas.semantic.DaxAgentInput",
        output_schema="app.schemas.semantic.DaxAgentOutput",
        allowed_tools=[
            "generate_measures", "validate_measure", "evaluate_measure_result",
            "load_artifact", "record_audit",
        ],
        uses_llm=True,
        policy=AgentPolicy(
            timeout_seconds=180, max_retries=1, max_usd=0.40, max_llm_calls=1,
            on_exhausted="degrade_to_heuristic",
        ),
    )

    def run(self, payload: DaxAgentInput, ctx: AgentContext) -> DaxAgentOutput:
        from ..services import repository as repo

        model = repo.load_artifact(
            ctx.session, payload.project_id, repo.ArtifactKind.MODEL, SemanticModel
        )
        if model is None:
            raise ValueError(
                "No semantic model exists. Run the semantic model agent first."
            )
        requirements = repo.load_artifact(
            ctx.session, payload.project_id, repo.ArtifactKind.REQUIREMENTS, RequirementsSpec
        )

        result = ctx.tools.call(
            "generate_measures", model=model, requirements=requirements,
            kpi_ids=payload.kpi_ids, qa_feedback=payload.qa_feedback,
        )
        measures: list[MeasureDefinition] = result["measures"]
        rejected: list[dict[str, str]] = result["rejected"]
        notes: list[str] = list(result.get("notes", []))

        ctx.note(
            f"{len(measures)} measure(s) generated and validated, "
            f"{len(rejected)} rejected."
        )

        # Gate 3: evaluate each measure against the real data where possible.
        probe = model.model_copy(update={"measures": model.measures + measures})
        evaluated = unsupported = failed = 0
        for measure in measures:
            outcome = ctx.tools.call(
                "evaluate_measure_result", ctx.session, payload.project_id, measure, probe
            )
            if outcome.status.value == "pass":
                evaluated += 1
                ctx.note(f"  {measure.name} = {outcome.actual} (DuckDB over real data)")
            elif outcome.status.value == "not_supported":
                unsupported += 1
                ctx.note(
                    f"  {measure.name}: NOT evaluated -- {outcome.actual}. "
                    f"Syntax and references were checked; the number was not."
                )
            else:
                failed += 1
                ctx.note(f"  {measure.name}: evaluation FAILED -- {outcome.actual}")
            repo.record_audit(
                ctx.session, project_id=payload.project_id, actor=self.spec.name.value,
                action=AuditAction.MEASURE_VALIDATED, target=measure.name,
                summary=f"{measure.name}: {outcome.status.value} via {outcome.engine.value}"
                        f" -- {outcome.actual[:120]}",
                severity=Severity.HIGH if outcome.status.is_blocking else Severity.INFO,
                detail={"engine": outcome.engine.value, "status": outcome.status.value},
            )

        notes.append(
            f"Evaluation against the prepared data: {evaluated} measure(s) returned a "
            f"real value, {unsupported} were outside the evaluable DAX subset (checked "
            f"for syntax and references only), {failed} failed to evaluate."
        )

        for measure in measures:
            repo.record_audit(
                ctx.session, project_id=payload.project_id, actor=self.spec.name.value,
                action=AuditAction.MEASURE_GENERATED, target=measure.name,
                summary=f"Generated {measure.name}: {measure.expression[:160]}",
                detail={"expression": measure.expression, "kpi_id": measure.kpi_id},
            )

        try:
            narrative = self._narrate(measures, model, ctx)
        except NoHeuristicAvailable as exc:
            ctx.note(f"No LLM narrative ({exc}). Generated explanations stand.")
            narrative = None
        except Exception as exc:  # noqa: BLE001
            ctx.note(f"LLM narrative failed ({type(exc).__name__}: {exc}).")
            narrative = None

        if narrative is not None:
            for measure in measures:
                description = narrative.descriptions.get(measure.name)
                if description:
                    measure.description = description.strip()
                    if measure.provenance:
                        measure.provenance.origin = Origin.LLM
                for assumption in narrative.extra_assumptions.get(measure.name, []):
                    if assumption not in measure.assumptions:
                        measure.assumptions.append(assumption)
            notes.extend(narrative.concerns)

        return DaxAgentOutput(measures=measures, rejected=rejected, notes=notes)

    def _narrate(
        self, measures: list[MeasureDefinition], model: SemanticModel, ctx: AgentContext
    ) -> MeasureNarrative | None:
        if not measures:
            return None
        prompt = (
            "# Generated measures\n\n"
            + "\n".join(
                f"## {m.name}\n```dax\n{m.expression}\n```\n"
                f"Current explanation: {m.description}\n"
                f"Aggregation behaviour: {m.aggregation_behaviour}"
                for m in measures
            )
            + "\n\n# Model\n"
            + "\n".join(
                f"- {t.name} ({t.role.value}): "
                f"{', '.join(c.name for c in t.columns if not c.is_hidden)[:160]}"
                for t in model.tables
            )
            + "\n\nWrite a plain-English explanation of each measure for a business "
              "reader: what it means, and what to watch out for when slicing it. Add any "
              "assumption the current text misses. Raise a concern if a measure looks "
              "wrong -- do not rewrite the DAX."
        )
        response = ask(ctx, StructuredRequest(
            purpose="dax.narrate", system=SYSTEM, prompt=prompt,
            output_model=MeasureNarrative, facts={"count": len(measures)},
            max_output_tokens=2400,
        ))
        return response.value  # type: ignore[return-value]
