"""QA Agent -- Step 8.

Independent by construction: it recomputes every verdict from the real data
and the stored artifacts rather than trusting any agent's self-report, and it
uses **no language model at all**. A reviewer whose verdict could be talked
out of a failure is not a reviewer.

On a blocking failure it emits ``ReworkRequest`` records naming the
responsible agent, which the orchestrator turns into real tasks -- so QA can
genuinely send work back.
"""
from __future__ import annotations

from ..schemas.agent import AgentName, AgentPolicy, AgentSpec, AuditAction
from ..schemas.common import Severity
from ..schemas.qa import QaAgentInput, QaAgentOutput, TestStatus, ValidationReport
from .base import Agent, AgentContext


class QaAgent(Agent[QaAgentInput, QaAgentOutput]):
    input_model = QaAgentInput
    output_model = QaAgentOutput
    spec = AgentSpec(
        name=AgentName.QA,
        purpose="Independently validate the pipeline's output against the real data: "
                "row reconciliation, referential integrity, cardinality, double "
                "counting, DAX syntax/references/execution, totals against directly "
                "computed sums, visual bindings, KPI coverage and unresolved "
                "assumptions. Returns failing work to the responsible agent.",
        input_schema="app.schemas.qa.QaAgentInput",
        output_schema="app.schemas.qa.QaAgentOutput",
        allowed_tools=["run_qa", "load_artifact", "record_audit"],
        uses_llm=False,
        policy=AgentPolicy(timeout_seconds=300, max_retries=1, max_usd=0.0, max_llm_calls=0),
    )

    def run(self, payload: QaAgentInput, ctx: AgentContext) -> QaAgentOutput:
        from ..services import repository as repo

        report: ValidationReport = ctx.tools.call(
            "run_qa", ctx.session, payload, actor=self.spec.name.value
        )

        counts = report.counts
        ctx.note(
            f"{len(report.results)} check(s): "
            + ", ".join(f"{v} {k}" for k, v in counts.items() if v)
        )
        ctx.note(f"engines actually used: {report.engines_used()}")
        for result in report.results:
            if result.status is TestStatus.PASS:
                continue
            marker = {
                TestStatus.FAIL: "FAIL", TestStatus.ERROR: "ERROR",
                TestStatus.WARN: "warn", TestStatus.SKIPPED: "skip",
                TestStatus.NOT_SUPPORTED: "n/a ",
            }[result.status]
            ctx.note(
                f"  [{marker}] {result.test_name}: expected {result.expected[:70]} "
                f"| actual {result.actual[:70]}"
            )

        notes: list[str] = []
        if report.passed:
            notes.append(
                "No blocking failure. Note what this does and does not establish: "
                + report.validation_scope_note
            )
        else:
            notes.append(
                f"{len(report.blocking_failures)} blocking failure(s). The pipeline's "
                f"output should not be trusted until they are addressed."
            )
        for rework in report.rework_requests:
            ctx.note(f"  rework -> {rework.target_agent}: {rework.reason[:160]}")
            notes.append(f"Returned to {rework.target_agent}: {rework.reason}")

        repo.record_audit(
            ctx.session, project_id=payload.project_id, actor=self.spec.name.value,
            action=AuditAction.QA_RUN, target=report.id,
            severity=Severity.HIGH if not report.passed else Severity.INFO,
            summary=f"QA: {counts.get('pass', 0)} passed, {counts.get('fail', 0)} failed, "
                    f"{counts.get('warn', 0)} warned, "
                    f"{counts.get('not_supported', 0)} not evaluable, "
                    f"{counts.get('skipped', 0)} skipped.",
            detail={"counts": counts, "engines": report.engines_used(),
                    "rework": [r.target_agent for r in report.rework_requests]},
        )
        return QaAgentOutput(report=report, notes=notes)
