"""Deterministic rules behind ``HeuristicProvider``.

Each rule takes the agent's ``facts`` dict and returns an instance of the
requested output model. Rules delegate to the real services -- they are
dispatchers, not a second implementation -- so behaviour with and without an
LLM differs only in the *interpretive* steps, never in the computation.

A purpose with no rule here raises ``NoHeuristicAvailable`` so the caller
degrades visibly instead of receiving invented content.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel


def _requirements(facts: dict[str, Any], model: type[BaseModel]) -> BaseModel:
    from ..schemas.requirements import RequirementsAgentInput, RequirementsAgentOutput
    from ..services.requirements_builder import build_minutes, build_requirements

    payload = RequirementsAgentInput.model_validate(facts["payload"])
    minutes = build_minutes(
        payload.transcript_text, project_id=payload.project_id,
        source_file_id=payload.transcript_source_file_id,
    )
    spec = build_requirements(payload, minutes)
    notes = []
    if payload.transcript_text and minutes is None:
        notes.append(
            "A transcript was supplied but no speaker structure could be found, so no "
            "minutes were produced. Minutes need lines of the form 'Name: text'."
        )
    if minutes:
        notes.append(
            f"Minutes extracted deterministically: {len(minutes.decisions)} decision(s), "
            f"{len(minutes.action_items)} action item(s), "
            f"{len(minutes.open_questions)} open question(s)."
        )
    return RequirementsAgentOutput(spec=spec, minutes=minutes, notes=notes)  # type: ignore[return-value]


def _prep_plan(facts: dict[str, Any], model: type[BaseModel]) -> BaseModel:
    from ..schemas.prep import PrepAgentOutput
    from ..services.prep.planner import plan_transformations

    plan = plan_transformations(
        profile=facts["profile"], requirements=facts.get("requirements"),
        project_id=facts["project_id"],
        user_overrides=facts.get("user_mapping_overrides") or {},
    )
    return PrepAgentOutput(  # type: ignore[return-value]
        plan=plan,
        notes=[
            f"{len(plan.mappings)} column mapping(s), {len(plan.steps)} step(s), "
            f"{len(plan.join_proposals)} join proposal(s) derived from measured evidence."
        ],
    )


def _semantic_model(facts: dict[str, Any], model: type[BaseModel]) -> BaseModel:
    from ..schemas.semantic import SemanticModelAgentOutput
    from ..services.semantic.designer import design_model

    out = design_model(
        project_id=facts["project_id"], datasets=facts["datasets"],
        profile=facts.get("profile"), requirements=facts.get("requirements"),
        plan=facts.get("plan"),
    )
    return SemanticModelAgentOutput.model_validate(out)  # type: ignore[return-value]


def _measures(facts: dict[str, Any], model: type[BaseModel]) -> BaseModel:
    from ..schemas.semantic import DaxAgentOutput
    from ..services.dax.generator import generate_measures

    return DaxAgentOutput.model_validate(  # type: ignore[return-value]
        generate_measures(
            model=facts["model"], requirements=facts.get("requirements"),
            kpi_ids=facts.get("kpi_ids") or [],
            qa_feedback=facts.get("qa_feedback") or [],
        )
    )


def _report(facts: dict[str, Any], model: type[BaseModel]) -> BaseModel:
    from ..schemas.report import ReportAgentOutput
    from ..services.report.designer import design_report

    return ReportAgentOutput.model_validate(  # type: ignore[return-value]
        design_report(
            project_id=facts["project_id"], model=facts["model"],
            requirements=facts.get("requirements"),
            design_preferences=facts.get("design_preferences", ""),
            max_pages=facts.get("max_pages", 5),
            qa_feedback=facts.get("qa_feedback") or [],
        )
    )


def _improvements(facts: dict[str, Any], model: type[BaseModel]) -> BaseModel:
    from ..schemas.qa import ImprovementAgentOutput
    from ..services.qa.improver import suggest_improvements

    return ImprovementAgentOutput.model_validate(  # type: ignore[return-value]
        suggest_improvements(**facts)
    )


#: purpose -> rule. The keys are the ``StructuredRequest.purpose`` values
#: agents use, so adding an agent means adding exactly one entry here.
HEURISTICS: dict[str, Callable[[dict[str, Any], type[BaseModel]], BaseModel]] = {
    "requirements.extract": _requirements,
    "prep.plan": _prep_plan,
    "semantic.design": _semantic_model,
    "dax.generate": _measures,
    "report.design": _report,
    "improvement.suggest": _improvements,
}
