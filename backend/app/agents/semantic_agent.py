"""Semantic Model Agent -- Step 5."""
from __future__ import annotations

from pydantic import Field

from ..llm.provider import NoHeuristicAvailable, StructuredRequest, ask
from ..schemas.agent import AgentName, AgentPolicy, AgentSpec
from ..schemas.common import Contract, Origin, Severity
from ..schemas.prep import TransformationPlan
from ..schemas.requirements import RequirementsSpec
from ..schemas.semantic import (
    SemanticModel, SemanticModelAgentInput, SemanticModelAgentOutput,
)
from ..schemas.source import SourceDataProfile
from .base import Agent, AgentContext

SYSTEM = """You are a senior Power BI data modeller reviewing a proposed \
semantic model.

You may improve table and column descriptions and the stated rationale. You \
must not change roles, relationships or cardinalities: those were derived from \
measured key uniqueness and value overlap in the real data. If you believe a \
role or relationship is wrong, raise it as a concern with your reasoning -- it \
will be shown to the user for a decision."""


class ModelCommentary(Contract):
    table_descriptions: dict[str, str] = Field(
        default_factory=dict, description="table name -> one-sentence business description"
    )
    design_summary: str = ""
    concerns: list[str] = Field(default_factory=list, max_length=8)
    suggested_display_folders: dict[str, str] = Field(default_factory=dict)


class SemanticModelAgent(Agent[SemanticModelAgentInput, SemanticModelAgentOutput]):
    input_model = SemanticModelAgentInput
    output_model = SemanticModelAgentOutput
    spec = AgentSpec(
        name=AgentName.SEMANTIC,
        purpose="Propose a maintainable star-schema semantic model from the prepared "
                "datasets: fact/dimension roles from measured key uniqueness, "
                "relationships only from measured overlap, with ambiguity, "
                "double-counting and referential-integrity risks reported rather than "
                "resolved silently.",
        input_schema="app.schemas.semantic.SemanticModelAgentInput",
        output_schema="app.schemas.semantic.SemanticModelAgentOutput",
        allowed_tools=["design_model", "build_diagram", "list_datasets", "load_artifact"],
        uses_llm=True,
        policy=AgentPolicy(
            timeout_seconds=120, max_retries=1, max_usd=0.25, max_llm_calls=1,
            on_exhausted="degrade_to_heuristic",
        ),
    )

    def run(
        self, payload: SemanticModelAgentInput, ctx: AgentContext
    ) -> SemanticModelAgentOutput:
        from ..services import repository as repo

        datasets = ctx.tools.call("list_datasets", ctx.session, payload.project_id)
        if not datasets:
            raise ValueError(
                "No prepared datasets exist. Run the transformation step before modelling."
            )
        if payload.datasets:
            wanted = set(payload.datasets)
            datasets = [d for d in datasets if d["name"] in wanted] or datasets

        # one record per dataset name, newest version first
        seen: set[str] = set()
        unique = []
        for d in datasets:
            if d["name"] in seen:
                continue
            seen.add(d["name"])
            unique.append(d)

        profile = repo.load_artifact(
            ctx.session, payload.project_id, repo.ArtifactKind.PROFILE, SourceDataProfile
        )
        requirements = repo.load_artifact(
            ctx.session, payload.project_id, repo.ArtifactKind.REQUIREMENTS, RequirementsSpec
        )
        plan = repo.load_artifact(
            ctx.session, payload.project_id, repo.ArtifactKind.PLAN, TransformationPlan
        )

        result = ctx.tools.call(
            "design_model", project_id=payload.project_id, datasets=unique,
            profile=profile, requirements=requirements, plan=plan,
        )
        model: SemanticModel = result["model"]
        diagram = result["diagram"]

        ctx.note(
            f"Shape: {model.schema_shape}; {len(model.tables)} table(s), "
            f"{len(model.relationships)} relationship(s)."
        )
        for table in model.tables:
            ctx.note(f"  {table.name} [{table.role.value}] {table.grain}")
        for relationship in model.relationships:
            flag = " NEEDS APPROVAL" if relationship.requires_approval else ""
            ctx.note(
                f"  rel {relationship.key} {relationship.cardinality.value} "
                f"({relationship.match_share:.1%} matched){flag}"
            )
        for issue in model.issues:
            if issue.severity in {Severity.CRITICAL, Severity.HIGH}:
                ctx.note(f"  [{issue.severity.value}] {issue.code}: {issue.message}")

        try:
            commentary = self._review(model, ctx)
        except NoHeuristicAvailable as exc:
            ctx.note(f"No LLM review ({exc}). The derived model stands unchanged.")
            commentary = None
        except Exception as exc:  # noqa: BLE001
            ctx.note(f"LLM review failed ({type(exc).__name__}: {exc}); model unchanged.")
            commentary = None

        notes = list(result.get("notes", []))
        if commentary is not None:
            for table in model.tables:
                description = commentary.table_descriptions.get(table.name)
                if description:
                    table.description = description.strip()
            if commentary.design_summary:
                model.design_rationale = (
                    f"{commentary.design_summary} {model.design_rationale}"
                )
                if model.provenance:
                    model.provenance.origin = Origin.LLM
            notes.extend(commentary.concerns)
            ctx.note(f"LLM review added {len(commentary.concerns)} concern(s).")

        return SemanticModelAgentOutput(model=model, diagram=diagram, notes=notes)

    def _review(self, model: SemanticModel, ctx: AgentContext) -> ModelCommentary | None:
        prompt = (
            "# Proposed semantic model\n\n"
            + "\n".join(
                f"## {t.name} ({t.role.value}, {t.row_count:,} rows)\n"
                f"Grain: {t.grain}\nWhy: {t.rationale}\n"
                f"Columns: {', '.join(c.name for c in t.columns if not c.is_hidden)}"
                for t in model.tables
            )
            + "\n\n## Relationships\n"
            + ("\n".join(
                f"- {r.key} {r.cardinality.value}, {r.match_share:.1%} of keys matched. "
                f"{r.rationale}"
                for r in model.relationships
            ) or "(none)")
            + "\n\n## Warnings already detected\n"
            + ("\n".join(f"- [{i.severity.value}] {i.message}" for i in model.issues)
               or "(none)")
            + "\n\nWrite a one-sentence business description for each table and a short "
              "design summary. Raise concerns if a role or relationship looks wrong, with "
              "your reasoning -- do not change them."
        )
        response = ask(ctx, StructuredRequest(
            purpose="semantic.review", system=SYSTEM, prompt=prompt,
            output_model=ModelCommentary, facts={"model_id": model.id},
            max_output_tokens=1800,
        ))
        return response.value  # type: ignore[return-value]
