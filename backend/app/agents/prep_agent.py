"""Data Preparation Agent -- Step 4.

Two task kinds share this agent:

* ``plan_transformations`` -- read the profile, propose mappings, steps and
  joins. Nothing is applied.
* ``execute_transformations`` -- apply the **approved** steps and produce the
  prepared datasets, lineage, before/after quality and the Power Query M.

The planning is deterministic. An LLM, when configured, is given one narrow
job: naming and explaining. It may improve a step's title and reason, and
suggest a better canonical name for a column the role vocabulary did not
recognise -- it cannot add, remove or re-parameterise a step, because the
plan is what gets executed.
"""
from __future__ import annotations

from pydantic import Field

from ..llm.provider import NoHeuristicAvailable, StructuredRequest, ask
from ..schemas.agent import AgentName, AgentPolicy, AgentSpec
from ..schemas.common import Confidence, Contract, Origin
from ..schemas.prep import (
    PrepAgentInput, PrepAgentOutput, TransformationExecution, TransformationPlan,
)
from ..schemas.requirements import RequirementsSpec
from ..schemas.source import SourceDataProfile
from .base import Agent, AgentContext

SYSTEM = """You are a senior data engineer reviewing a generated data \
preparation plan.

You may improve wording and propose better canonical column names. You must \
not invent transformations: the step list is fixed and has already been \
derived from measurements of the real data. If a proposed canonical name \
would be misleading, say so rather than renaming it to something vaguer."""


class PlanCommentary(Contract):
    """The narrow surface an LLM may contribute to a plan."""

    summary: str = Field(default="", description="2-4 sentences for a reviewer.")
    step_titles: dict[str, str] = Field(
        default_factory=dict, description="step id -> clearer title"
    )
    canonical_name_suggestions: dict[str, str] = Field(
        default_factory=dict,
        description="'table.column' -> a better canonical name, only for columns listed "
        "as unrecognised.",
    )
    concerns: list[str] = Field(default_factory=list, max_length=8)


class PrepPlanInput(PrepAgentInput):
    pass


class DataPreparationAgent(Agent[PrepAgentInput, PrepAgentOutput]):
    input_model = PrepAgentInput
    output_model = PrepAgentOutput
    spec = AgentSpec(
        name=AgentName.PREP,
        purpose="Map inconsistent source columns to canonical names, plan reproducible "
                "transformations with stated reasons and risks, propose joins from measured "
                "key overlap, and execute the approved plan with full lineage.",
        input_schema="app.schemas.prep.PrepAgentInput",
        output_schema="app.schemas.prep.PrepAgentOutput",
        allowed_tools=[
            "plan_transformations", "load_artifact", "list_datasets", "record_audit",
        ],
        uses_llm=True,
        policy=AgentPolicy(
            timeout_seconds=180, max_retries=1, max_usd=0.30, max_llm_calls=1,
            on_exhausted="degrade_to_heuristic",
        ),
    )

    def run(self, payload: PrepAgentInput, ctx: AgentContext) -> PrepAgentOutput:
        from ..services import repository as repo

        profile = repo.load_artifact(
            ctx.session, payload.project_id, repo.ArtifactKind.PROFILE, SourceDataProfile
        )
        if profile is None:
            raise ValueError(
                "No source profile exists for this project. Run the profiling agent first."
            )
        requirements = repo.load_artifact(
            ctx.session, payload.project_id, repo.ArtifactKind.REQUIREMENTS, RequirementsSpec
        )

        plan: TransformationPlan = ctx.tools.call(
            "plan_transformations",
            profile=profile, requirements=requirements,
            project_id=payload.project_id,
            user_overrides=payload.user_mapping_overrides,
        )

        ctx.note(
            f"{len(plan.mappings)} mapping(s), {len(plan.steps)} step(s), "
            f"{len(plan.join_proposals)} join proposal(s), "
            f"{len(plan.steps_awaiting_approval)} step(s) need approval."
        )
        for step in plan.ordered_steps():
            if step.requires_approval:
                ctx.note(
                    f"  APPROVAL NEEDED [{step.risk.value}] {step.title}: {step.expected_effect}"
                )
        for join in plan.join_proposals:
            flag = "FAN-OUT RISK " if join.fan_out_risk else ""
            ctx.note(
                f"  join {flag}{join.left_table}.{join.left_keys[0]} -> "
                f"{join.right_table}.{join.right_keys[0]} "
                f"({join.match_share:.1%} matched, {join.confidence.value} confidence)"
            )

        notes: list[str] = []
        try:
            commentary = self._review(plan, profile, ctx)
        except NoHeuristicAvailable as exc:
            ctx.note(f"No LLM review ({exc}). The deterministic plan stands unchanged.")
            commentary = None
        except Exception as exc:  # noqa: BLE001
            ctx.note(f"LLM review failed ({type(exc).__name__}: {exc}); plan unchanged.")
            commentary = None

        if commentary is not None:
            applied = 0
            by_id = {s.id: s for s in plan.steps}
            for step_id, title in commentary.step_titles.items():
                step = by_id.get(step_id)
                if step and title.strip():
                    step.title = title.strip()
                    applied += 1
            renamed = 0
            for ref, suggestion in commentary.canonical_name_suggestions.items():
                mapping = next(
                    (m for m in plan.mappings
                     if f"{m.source_table}.{m.source_column}" == ref), None
                )
                # Only columns the role vocabulary did not recognise are open
                # to renaming, and the result still needs approval.
                if mapping and mapping.confidence is not Confidence.HIGH and suggestion.strip():
                    mapping.canonical_name = suggestion.strip()
                    mapping.reason = (
                        f"Proposed by a language model from the column's contents and the "
                        f"project context (previously: {mapping.reason})"
                    )
                    mapping.confidence = Confidence.LOW
                    renamed += 1
            if commentary.summary:
                plan.summary = f"{commentary.summary} {plan.summary}"
            notes.extend(commentary.concerns)
            if applied or renamed:
                plan.provenance.origin = Origin.LLM
                plan.provenance.rationale += (
                    f" A language model improved {applied} step title(s) and proposed "
                    f"{renamed} canonical name(s); every executable parameter remains "
                    f"the deterministic value."
                )
            ctx.note(f"LLM review: {applied} title(s) improved, {renamed} name(s) proposed, "
                     f"{len(commentary.concerns)} concern(s) raised.")

        return PrepAgentOutput(plan=plan, notes=notes)

    # ------------------------------------------------------------------
    def _review(
        self, plan: TransformationPlan, profile: SourceDataProfile, ctx: AgentContext
    ) -> PlanCommentary | None:
        unrecognised = [
            f"{m.source_table}.{m.source_column} (currently '{m.canonical_name}')"
            for m in plan.mappings if m.confidence is not Confidence.HIGH
        ][:40]
        steps = "\n".join(
            f"- {s.id} [{s.risk.value}] {s.op.value} on {s.target_table}: "
            f"{s.title} -- {s.reason}"
            for s in plan.ordered_steps()[:50]
        )
        prompt = (
            f"# Generated data preparation plan\n\n## Steps\n{steps}\n\n"
            f"## Columns whose meaning was not recognised\n"
            + ("\n".join(f"- {u}" for u in unrecognised) or "(none)")
            + "\n\n## Source tables\n"
            + "\n".join(
                f"- {t.table_name}: {t.row_count} rows; "
                f"columns {', '.join(c.name for c in t.columns[:12])}"
                for t in profile.tables
            )
            + "\n\n## Measured problems\n"
            + "\n".join(f"- {i.code} at {i.location}: {i.message}"
                        for i in profile.cross_table_issues[:12])
            + "\n\nReview the plan. Improve step titles where they are unclear. Propose a "
              "canonical name only for the unrecognised columns above. Raise concerns about "
              "anything that looks wrong -- especially a step that could lose data."
        )
        response = ask(ctx, StructuredRequest(
            purpose="prep.review", system=SYSTEM, prompt=prompt,
            output_model=PlanCommentary, facts={"plan_id": plan.id},
            max_output_tokens=2048,
        ))
        return response.value  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Execution is a separate task kind on the same agent.
# --------------------------------------------------------------------------
class ExecuteInput(Contract):
    project_id: str
    plan_id: str = ""
    profile_id: str = ""


class ExecuteOutput(Contract):
    execution: TransformationExecution
    quality_delta: dict = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class TransformationExecutorAgent(Agent[ExecuteInput, ExecuteOutput]):
    """Applies the approved plan. Entirely deterministic: no LLM, by design --
    nothing a model says should be able to change what happens to the data."""

    input_model = ExecuteInput
    output_model = ExecuteOutput
    spec = AgentSpec(
        name=AgentName.PREP,
        purpose="Execute the approved transformation plan deterministically, producing "
                "prepared datasets, column lineage, before/after data-quality reports and "
                "the equivalent Power Query M.",
        input_schema="app.agents.prep_agent.ExecuteInput",
        output_schema="app.agents.prep_agent.ExecuteOutput",
        allowed_tools=["execute_plan", "load_frames", "load_artifact", "record_audit"],
        uses_llm=False,
        policy=AgentPolicy(timeout_seconds=300, max_retries=1, max_usd=0.0, max_llm_calls=0),
    )

    def run(self, payload: ExecuteInput, ctx: AgentContext) -> ExecuteOutput:
        from ..services import repository as repo
        from ..services.prep.quality import compare

        plan = repo.load_artifact(
            ctx.session, payload.project_id, repo.ArtifactKind.PLAN, TransformationPlan
        )
        if plan is None:
            raise ValueError(
                "No transformation plan exists. Run plan_transformations first."
            )

        frames, _, _ = ctx.tools.call(
            "load_frames", ctx.session, payload.project_id, actor=self.spec.name.value
        )
        if not frames:
            raise ValueError("No readable source data was found for this project.")

        execution: TransformationExecution = ctx.tools.call(
            "execute_plan", ctx.session, payload.project_id, plan,
            frames=frames, actor=self.spec.name.value,
        )

        for line in execution.log:
            ctx.note(line)
        if not execution.succeeded:
            raise RuntimeError(execution.error or "The transformation failed.")

        delta = (
            compare(execution.quality_before, execution.quality_after)
            if execution.quality_before and execution.quality_after else {}
        )
        notes: list[str] = []
        if execution.skipped_steps:
            notes.append(
                f"{len(execution.skipped_steps)} step(s) were skipped because they are "
                f"unapproved or were rejected. The datasets reflect only the approved steps."
            )
        if delta.get("regressed"):
            notes.append(
                "Some quality metrics got worse: " + "; ".join(delta["regressed"][:4])
            )
        return ExecuteOutput(execution=execution, quality_delta=delta, notes=notes)
