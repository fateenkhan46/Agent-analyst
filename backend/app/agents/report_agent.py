"""Report Design Agent (Step 7) and the PBIP export agent (Step 10)."""
from __future__ import annotations

from pydantic import Field

from ..llm.provider import NoHeuristicAvailable, StructuredRequest, ask
from ..schemas.agent import AgentName, AgentPolicy, AgentSpec
from ..schemas.common import Contract, Origin, Severity
from ..schemas.prep import TransformationExecution
from ..schemas.report import (
    ExportArtifact, ReportAgentInput, ReportAgentOutput, ReportSpec,
)
from ..schemas.requirements import RequirementsSpec
from ..schemas.semantic import SemanticModel
from .base import Agent, AgentContext

SYSTEM = """You are a senior Power BI report designer reviewing a generated \
report layout.

You may improve page and visual titles and sharpen the stated rationale. You \
must not add visuals: every visual here exists because a specific reporting \
question needs it, and padding a page with extra charts makes a report worse. \
If a page is missing something a reader genuinely needs, say so as a concern."""


class ReportCommentary(Contract):
    page_titles: dict[str, str] = Field(default_factory=dict)
    visual_titles: dict[str, str] = Field(
        default_factory=dict, description="visual name -> clearer title"
    )
    narrative: str = Field(default="", description="How to read this report, 2-4 sentences.")
    concerns: list[str] = Field(default_factory=list, max_length=8)


class ReportDesignAgent(Agent[ReportAgentInput, ReportAgentOutput]):
    input_model = ReportAgentInput
    output_model = ReportAgentOutput
    spec = AgentSpec(
        name=AgentName.REPORT,
        purpose="Design report pages and visuals that answer the stated reporting "
                "questions, binding only to fields that exist in the approved model, "
                "with the reason each visual type suits its question.",
        input_schema="app.schemas.report.ReportAgentInput",
        output_schema="app.schemas.report.ReportAgentOutput",
        allowed_tools=["design_report", "load_artifact", "record_audit"],
        uses_llm=True,
        policy=AgentPolicy(
            timeout_seconds=120, max_retries=1, max_usd=0.30, max_llm_calls=1,
            on_exhausted="degrade_to_heuristic",
        ),
    )

    def run(self, payload: ReportAgentInput, ctx: AgentContext) -> ReportAgentOutput:
        from ..services import repository as repo
        from ..services.report.designer import design_report

        model = repo.load_artifact(
            ctx.session, payload.project_id, repo.ArtifactKind.MODEL, SemanticModel
        )
        if model is None:
            raise ValueError("No semantic model exists. Run the model agent first.")
        requirements = repo.load_artifact(
            ctx.session, payload.project_id, repo.ArtifactKind.REQUIREMENTS, RequirementsSpec
        )

        result = design_report(
            project_id=payload.project_id, model=model, requirements=requirements,
            design_preferences=payload.design_preferences,
            max_pages=payload.max_pages or 5, qa_feedback=payload.qa_feedback,
        )
        report: ReportSpec = result["report"]
        notes: list[str] = list(result.get("notes", []))

        ctx.note(
            f"{len(report.pages)} page(s), {report.visual_count} visual(s), "
            f"{len(report.unmapped_kpis)} unmapped KPI(s)."
        )
        for page in report.pages:
            ctx.note(f"  {page.display_name}: {len(page.visuals)} visual(s) -- {page.purpose}")
            for visual in page.visuals:
                ctx.note(
                    f"    {visual.visual_type.value}: {visual.title} "
                    f"<- {', '.join(b.reference for b in visual.bindings) or 'text'}"
                )
        for issue in report.issues:
            if issue.severity in {Severity.CRITICAL, Severity.HIGH}:
                ctx.note(f"  [{issue.severity.value}] {issue.code}: {issue.message}")

        try:
            commentary = self._review(report, model, ctx)
        except NoHeuristicAvailable as exc:
            ctx.note(f"No LLM review ({exc}). The generated layout stands.")
            commentary = None
        except Exception as exc:  # noqa: BLE001
            ctx.note(f"LLM review failed ({type(exc).__name__}: {exc}); layout unchanged.")
            commentary = None

        if commentary is not None:
            for page in report.pages:
                better = commentary.page_titles.get(page.name)
                if better:
                    page.display_name = better.strip()
                for visual in page.visuals:
                    title = commentary.visual_titles.get(visual.name)
                    if title:
                        visual.title = title.strip()
            if commentary.narrative:
                report.design_notes = f"{commentary.narrative} {report.design_notes}"
                if report.provenance:
                    report.provenance.origin = Origin.LLM
            notes.extend(commentary.concerns)

        return ReportAgentOutput(report=report, notes=notes)

    def _review(
        self, report: ReportSpec, model: SemanticModel, ctx: AgentContext
    ) -> ReportCommentary | None:
        prompt = (
            "# Generated report\n\n"
            + "\n".join(
                f"## Page '{page.name}' -- {page.display_name}\n"
                f"Purpose: {page.purpose}\n"
                + "\n".join(
                    f"- {v.name} ({v.visual_type.value}): \"{v.title}\" "
                    f"answers \"{v.business_question}\" using "
                    f"{', '.join(b.reference for b in v.bindings) or 'text'}"
                    for v in page.visuals
                )
                for page in report.pages
            )
            + "\n\nImprove titles where they are unclear and write a short 'how to read "
              "this report' narrative. Raise concerns rather than adding visuals."
        )
        response = ask(ctx, StructuredRequest(
            purpose="report.review", system=SYSTEM, prompt=prompt,
            output_model=ReportCommentary, facts={"report_id": report.id},
            max_output_tokens=1600,
        ))
        return response.value  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
class ExportInput(Contract):
    project_id: str
    model_id: str = ""
    report_id: str = ""


class ExportOutput(Contract):
    artifacts: list[ExportArtifact] = Field(default_factory=list)
    verification: list[dict] = Field(default_factory=list)
    manual_steps: list[str] = Field(default_factory=list)
    manifest: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    succeeded: bool = False


class PbipExportAgent(Agent[ExportInput, ExportOutput]):
    """Writes the PBIP project and verifies it by re-reading it from disk.

    Deterministic by design: nothing an LLM says should be able to change the
    bytes of the deliverable. If verification fails, no export artifact is
    recorded, so the delivery page cannot show a download for a project that
    was not written correctly.
    """

    input_model = ExportInput
    output_model = ExportOutput
    spec = AgentSpec(
        name=AgentName.REPORT,
        purpose="Write a complete, editable PBIP project (TMDL semantic model, PBIR "
                "report, Power Query, theme and data), verify every file by re-reading "
                "it, and state precisely what remains to be done in Power BI Desktop.",
        input_schema="app.agents.report_agent.ExportInput",
        output_schema="app.agents.report_agent.ExportOutput",
        allowed_tools=["export_pbip", "load_artifact", "record_audit"],
        uses_llm=False,
        policy=AgentPolicy(timeout_seconds=180, max_retries=1, max_usd=0.0, max_llm_calls=0),
    )

    def run(self, payload: ExportInput, ctx: AgentContext) -> ExportOutput:
        from ..services import repository as repo

        model = repo.load_artifact(
            ctx.session, payload.project_id, repo.ArtifactKind.MODEL, SemanticModel
        )
        report = repo.load_artifact(
            ctx.session, payload.project_id, repo.ArtifactKind.REPORT, ReportSpec
        )
        if model is None or report is None:
            raise ValueError(
                "Both a semantic model and a report specification are required before "
                "the project can be exported."
            )
        execution = repo.load_artifact(
            ctx.session, payload.project_id, repo.ArtifactKind.EXECUTION,
            TransformationExecution,
        )
        project = repo.get_project(ctx.session, payload.project_id)

        artifact, verification, built = ctx.tools.call(
            "export_pbip", ctx.session, payload.project_id,
            model=model, report=report,
            project_name=(project.name if project else "Report"),
            execution=execution, actor=self.spec.name.value,
        )

        for check in verification.checks:
            if not check["passed"]:
                ctx.note(f"  VERIFY FAILED {check['check']}: {check['detail']}")
        ctx.note(
            f"{len(built.files)} file(s) written; "
            f"{sum(1 for c in verification.checks if c['passed'])}/"
            f"{len(verification.checks)} verification check(s) passed."
        )
        for warning in built.warnings:
            ctx.note(f"  warning: {warning}")
        for skipped in built.skipped_bindings:
            ctx.note(f"  binding not written: {skipped}")

        notes = list(built.warnings)
        if built.skipped_bindings:
            notes.append(
                f"{len(built.skipped_bindings)} visual binding(s) could not be resolved "
                f"against the model and were left out, so those visuals are absent "
                f"rather than broken."
            )
        if not verification.ok:
            notes.append(
                "The project was written but FAILED verification, so no download was "
                "registered: " + "; ".join(verification.failures[:4])
            )
            return ExportOutput(
                artifacts=[], verification=verification.checks,
                manual_steps=built.manual_steps, manifest=built.manifest,
                notes=notes, succeeded=False,
            )

        assert artifact is not None
        ctx.note(
            f"Export verified: {artifact.filename} ({artifact.size_bytes:,} bytes, "
            f"{artifact.file_count} file(s))."
        )
        for step in built.manual_steps:
            ctx.note(f"  remaining step: {step[:150]}")
        return ExportOutput(
            artifacts=[artifact], verification=verification.checks,
            manual_steps=built.manual_steps, manifest=artifact.manifest,
            notes=notes, succeeded=True,
        )
