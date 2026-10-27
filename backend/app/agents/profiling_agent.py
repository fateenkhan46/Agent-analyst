"""Data Profiling Agent -- Step 2.

Entirely deterministic: it delegates to ``app.services.profiling`` and does no
LLM reasoning at all. It is still a first-class agent because the orchestrator
needs it in the dependency graph, and because its executions, timings and
findings belong in the same audit trail as everything else.
"""
from __future__ import annotations

from pydantic import Field

from ..schemas.agent import AgentName, AgentPolicy, AgentSpec
from ..schemas.common import Contract
from ..schemas.source import SourceDataProfile
from .base import Agent, AgentContext


class ProfilingInput(Contract):
    project_id: str
    reprofile: bool = Field(
        default=False, description="Force a new profile even if one exists."
    )


class ProfilingOutput(Contract):
    profile: SourceDataProfile
    table_count: int = 0
    total_rows: int = 0
    issue_counts: dict[str, int] = Field(default_factory=dict)


class DataProfilingAgent(Agent[ProfilingInput, ProfilingOutput]):
    input_model = ProfilingInput
    output_model = ProfilingOutput
    spec = AgentSpec(
        name=AgentName.PROFILING,
        purpose="Measure the structure, types, formats, keys and quality problems of every "
                "uploaded source, without interpreting business meaning.",
        input_schema="app.agents.profiling_agent.ProfilingInput",
        output_schema="app.agents.profiling_agent.ProfilingOutput",
        allowed_tools=["profile_project", "load_frames"],
        uses_llm=False,
        policy=AgentPolicy(timeout_seconds=300, max_retries=1, max_usd=0.0, max_llm_calls=0),
    )

    def run(self, payload: ProfilingInput, ctx: AgentContext) -> ProfilingOutput:
        profile: SourceDataProfile = ctx.tools.call(
            "profile_project", ctx.session, payload.project_id, actor=self.spec.name.value
        )
        counts = profile.issue_counts()
        ctx.note(
            f"Profiled {len(profile.tables)} table(s), {profile.total_rows:,} rows, "
            f"{len(profile.key_overlaps)} candidate key pair(s)."
        )
        for t in profile.tables:
            ctx.note(
                f"  {t.table_name}: {t.row_count:,} rows x {t.column_count} cols, "
                f"naming={t.naming_convention or 'n/a'}, "
                f"pk candidates={t.candidate_primary_keys or 'none found'}"
            )
        if counts.get("critical"):
            ctx.note(f"{counts['critical']} CRITICAL data-quality finding(s) need attention.")
        return ProfilingOutput(
            profile=profile, table_count=len(profile.tables),
            total_rows=profile.total_rows, issue_counts=counts,
        )
