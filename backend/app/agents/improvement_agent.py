"""Improvement Agent -- Step 9.

Examines the requirements, transformations, model, measures and report and
proposes improvements, each with its rationale, measured evidence, expected
benefit and -- required, not optional -- its trade-offs.

It changes nothing. Every recommendation carries a machine-applicable payload
that is acted on only after approval, and applying one re-runs the responsible
agent so the change stays auditable.
"""
from __future__ import annotations

from pydantic import Field

from ..llm.provider import NoHeuristicAvailable, StructuredRequest, ask
from ..schemas.agent import AgentName, AgentPolicy, AgentSpec
from ..schemas.common import Confidence, Contract, Origin, Provenance, Severity
from ..schemas.qa import (
    ImprovementAgentInput, ImprovementAgentOutput, ImprovementArea,
    ImprovementRecommendation, ImprovementReport,
)
from .base import Agent, AgentContext

SYSTEM = """You are a senior analytics engineer reviewing a completed BI build.

Rules:
- Every recommendation needs a trade-off. A recommendation with no cost has \
usually not been thought through.
- Ground each one in the evidence supplied; do not speculate about data you \
cannot see.
- Do not propose changing an agreed business definition. Definitions come from \
the stakeholders, not from you."""


class ExtraImprovements(Contract):
    recommendations: list[dict] = Field(
        default_factory=list, max_length=8,
        description="Each: {area, title, rationale, evidence[], expected_benefit, "
                    "trade_offs[], effort, severity}",
    )
    summary: str = ""


class ImprovementAgent(Agent[ImprovementAgentInput, ImprovementAgentOutput]):
    input_model = ImprovementAgentInput
    output_model = ImprovementAgentOutput
    spec = AgentSpec(
        name=AgentName.IMPROVEMENT,
        purpose="Propose improvements to the transformations, model, measures and report "
                "with rationale, measured evidence, expected benefit and trade-offs. "
                "Changes nothing without approval.",
        input_schema="app.schemas.qa.ImprovementAgentInput",
        output_schema="app.schemas.qa.ImprovementAgentOutput",
        allowed_tools=["suggest_improvements", "load_artifact", "record_audit"],
        uses_llm=True,
        policy=AgentPolicy(
            timeout_seconds=150, max_retries=1, max_usd=0.30, max_llm_calls=1,
            on_exhausted="degrade_to_heuristic",
        ),
    )

    def run(
        self, payload: ImprovementAgentInput, ctx: AgentContext
    ) -> ImprovementAgentOutput:
        result = ctx.tools.call(
            "suggest_improvements", session=ctx.session, project_id=payload.project_id,
        )
        report: ImprovementReport = result["report"]
        notes: list[str] = list(result.get("notes", []))

        ctx.note(f"{len(report.recommendations)} recommendation(s): {report.by_area()}")
        for rec in report.recommendations:
            ctx.note(f"  [{rec.severity.value:8}] {rec.area.value:22} {rec.title[:78]}")

        try:
            extra = self._ask_for_more(report, ctx)
        except NoHeuristicAvailable as exc:
            ctx.note(f"No LLM suggestions ({exc}). Deterministic analysis only.")
            extra = None
        except Exception as exc:  # noqa: BLE001
            ctx.note(f"LLM suggestions failed ({type(exc).__name__}: {exc}).")
            extra = None

        if extra is not None:
            added = 0
            for raw in extra.recommendations:
                try:
                    area = ImprovementArea(raw.get("area", "additional_insight"))
                except ValueError:
                    area = ImprovementArea.ADDITIONAL_INSIGHT
                trade_offs = [t for t in (raw.get("trade_offs") or []) if str(t).strip()]
                if not raw.get("title") or not raw.get("rationale") or not trade_offs:
                    # The trade-off requirement is enforced, not merely requested.
                    continue
                report.recommendations.append(ImprovementRecommendation(
                    project_id=payload.project_id, area=area,
                    title=str(raw["title"])[:200],
                    rationale=str(raw["rationale"]),
                    evidence=[str(e) for e in (raw.get("evidence") or [])][:6],
                    expected_benefit=str(raw.get("expected_benefit", "")),
                    trade_offs=[str(t) for t in trade_offs][:6],
                    effort=str(raw.get("effort", "medium")),
                    severity=Severity(raw.get("severity", "low"))
                    if raw.get("severity") in {s.value for s in Severity}
                    else Severity.LOW,
                    provenance=Provenance(
                        origin=Origin.LLM, produced_by=self.spec.name.value,
                        confidence=Confidence.LOW,
                        rationale="Proposed by a language model from the supplied "
                                  "evidence; not derived from a measurement, so treat "
                                  "it as a suggestion to evaluate.",
                    ),
                ))
                added += 1
            rejected = len(extra.recommendations) - added
            if rejected:
                notes.append(
                    f"{rejected} model-proposed recommendation(s) were discarded for "
                    f"having no stated trade-off or no rationale."
                )
            if extra.summary:
                report.summary = f"{extra.summary} {report.summary}"
            ctx.note(f"LLM added {added} recommendation(s), discarded {rejected}.")

        return ImprovementAgentOutput(report=report, notes=notes)

    def _ask_for_more(
        self, report: ImprovementReport, ctx: AgentContext
    ) -> ExtraImprovements | None:
        from ..services import repository as repo
        from ..schemas.semantic import SemanticModel
        from ..schemas.source import SourceDataProfile

        model = repo.load_artifact(
            ctx.session, report.project_id, repo.ArtifactKind.MODEL, SemanticModel
        )
        profile = repo.load_artifact(
            ctx.session, report.project_id, repo.ArtifactKind.PROFILE, SourceDataProfile
        )
        prompt = (
            "# Already identified (do not repeat)\n"
            + "\n".join(f"- [{r.area.value}] {r.title}" for r in report.recommendations)
            + "\n\n# The model\n"
            + ("\n".join(
                f"- {t.name} ({t.role.value}, {t.row_count:,} rows): "
                f"{', '.join(c.name for c in t.columns if not c.is_hidden)[:140]}"
                for t in model.tables
            ) if model else "(none)")
            + "\n\n# Measures\n"
            + ("\n".join(f"- {m.name}: {m.expression}" for m in model.measures)
               if model else "(none)")
            + "\n\n# Measured data-quality findings\n"
            + ("\n".join(f"- {i.code} at {i.location}: {i.message}"
                         for i in profile.cross_table_issues[:10])
               if profile else "(none)")
            + "\n\nPropose up to 6 further improvements this analysis has missed. Each "
              "MUST include at least one trade-off; one without will be discarded."
        )
        response = ask(ctx, StructuredRequest(
            purpose="improvement.extra", system=SYSTEM, prompt=prompt,
            output_model=ExtraImprovements, facts={"project_id": report.project_id},
            max_output_tokens=2400,
        ))
        return response.value  # type: ignore[return-value]
