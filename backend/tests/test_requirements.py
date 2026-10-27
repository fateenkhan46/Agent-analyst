"""Requirements extraction and the Minutes of Meeting."""
from __future__ import annotations

import pytest

from app.schemas.common import Confidence
from app.schemas.requirements import RequirementsAgentInput
from app.services.requirements_builder import build_minutes, build_requirements
from app.services.transcript import (
    analyse, classify_action, classify_decision, classify_open_question, classify_risk,
)

TRANSCRIPT = """Cost Reporting Requirements
Date: 2026-03-04
Attendees: Dana Okoro (Finance Lead), Sam Petrov (Data), Lee Wu (Operations)

Dana Okoro: We need one number for project cost and one for variance.
Sam Petrov: Which way round is variance?
Dana Okoro: Budget Variance is Budget minus Actual Cost. Positive means under budget.
Sam Petrov: Understood.
Lee Wu: My timesheets mix hours and minutes.
Sam Petrov: Everything becomes hours.
Lee Wu: Thank you.
Dana Okoro: I'll confirm the currency treatment in writing by Friday.
Sam Petrov: What exchange rate date should I use?
Dana Okoro: That's an open question for Finance.
Lee Wu: I am worried the Q1 labour export may be incomplete.
Dana Okoro: Maybe we should also look at headcount, I'm not sure.
"""


def test_attendees_are_parsed_with_roles_and_measured_turns() -> None:
    minutes = build_minutes(TRANSCRIPT, project_id="p")
    assert minutes is not None
    by_name = {a.name: a for a in minutes.attendees}
    assert set(by_name) == {"Dana Okoro", "Sam Petrov", "Lee Wu"}
    assert by_name["Dana Okoro"].role == "Finance Lead"
    assert by_name["Dana Okoro"].speaking_turns == 5
    assert all(not a.inferred for a in minutes.attendees), "all three are declared"


def test_definitional_decision_is_captured_with_high_confidence() -> None:
    """The most important decision in a requirements meeting carries no
    directive language at all, so it needs its own classification."""
    minutes = build_minutes(TRANSCRIPT, project_id="p")
    assert minutes is not None
    variance = next(
        (d for d in minutes.decisions if "minus" in d.statement.lower()), None
    )
    assert variance is not None, [d.statement for d in minutes.decisions]
    assert variance.decided_by == "Dana Okoro"
    assert variance.confidence is Confidence.HIGH, "confirmed by another speaker"
    assert variance.spans and variance.spans[0].start_line > 0


def test_hedged_statements_are_not_decisions() -> None:
    minutes = build_minutes(TRANSCRIPT, project_id="p")
    assert minutes is not None
    assert not any("headcount" in d.statement.lower() for d in minutes.decisions), \
        "'maybe ... I'm not sure' must never be recorded as a decision"


def test_meeting_admin_is_not_a_decision() -> None:
    text = TRANSCRIPT + "\nDana Okoro: Let's wrap up.\n"
    minutes = build_minutes(text, project_id="p")
    assert minutes is not None
    assert not any("wrap up" in d.statement.lower() for d in minutes.decisions)


def test_action_items_capture_owner_and_due_date() -> None:
    minutes = build_minutes(TRANSCRIPT, project_id="p")
    assert minutes is not None
    action = next(
        (a for a in minutes.action_items if "currency" in a.description.lower()), None
    )
    assert action is not None, [a.description for a in minutes.action_items]
    assert action.owner == "Dana Okoro"
    assert action.due.lower() == "friday"


def test_unanswered_question_becomes_an_open_question() -> None:
    minutes = build_minutes(TRANSCRIPT, project_id="p")
    assert minutes is not None
    assert any("rate" in q.question.lower() for q in minutes.open_questions), \
        [q.question for q in minutes.open_questions]


def test_risks_are_captured() -> None:
    minutes = build_minutes(TRANSCRIPT, project_id="p")
    assert minutes is not None
    assert any("incomplete" in r.lower() for r in minutes.risks)


def test_no_transcript_means_no_minutes() -> None:
    """Minutes are never manufactured from nothing."""
    assert build_minutes("", project_id="p") is None
    assert build_minutes("   \n  ", project_id="p") is None


def test_unstructured_text_does_not_become_minutes() -> None:
    assert build_minutes("just a short note", project_id="p") is None


def test_minutes_render_to_markdown_with_citations() -> None:
    from app.services.mom import render_minutes

    minutes = build_minutes(TRANSCRIPT, project_id="p")
    assert minutes is not None
    md = render_minutes(minutes)
    assert "## Attendees" in md and "## Decisions" in md and "## Action items" in md
    assert "Dana Okoro" in md
    assert "lines" in md, "decisions must cite transcript lines"
    assert "deterministic rule-based extractor" in md, "provenance must be stated"


# --- requirements spec ---------------------------------------------------
def _payload(**kwargs) -> RequirementsAgentInput:
    base = dict(
        project_id="p", project_name="Cost", currency="USD", units="hours",
        stated_kpis=["Total Cost", "Budget Variance"],
        available_columns={
            "costs": ["project_id", "cost_amount", "transaction_date"],
            "register": ["project_id", "approved_budget"],
        },
    )
    base.update(kwargs)
    return RequirementsAgentInput(**base)  # type: ignore[arg-type]


def test_catalogue_kpis_get_grounded_definitions() -> None:
    spec = build_requirements(_payload(), None)
    total = spec.kpi("Total Cost")
    assert total is not None and total.definition
    assert total.aggregation.value == "sum"
    assert total.format_string


def test_unknown_kpi_is_queried_not_invented() -> None:
    """The central honesty rule for this step."""
    spec = build_requirements(_payload(stated_kpis=["Widget Throughput Index"]), None)
    kpi = spec.kpi("Widget Throughput Index")
    assert kpi is not None
    assert kpi.definition == "", "an unknown KPI must NOT receive a guessed definition"
    assert kpi.needs_confirmation
    assert any("Widget Throughput Index" in q.question for q in spec.open_questions)


def test_missing_source_data_for_a_kpi_raises_a_question() -> None:
    spec = build_requirements(
        _payload(
            stated_kpis=["Total Labour Hours"],
            available_columns={"costs": ["project_id", "cost_amount"]},
        ),
        None,
    )
    assert any("hours" in q.question.lower() for q in spec.open_questions), \
        [q.question for q in spec.open_questions]


def test_meeting_decision_is_attached_to_the_right_kpi_only() -> None:
    minutes = build_minutes(TRANSCRIPT, project_id="p")
    spec = build_requirements(
        _payload(stated_kpis=["Budget Variance", "Project Completion %"]), minutes
    )
    variance = spec.kpi("Budget Variance")
    completion = spec.kpi("Project Completion %")
    assert variance is not None and variance.source_decisions
    assert "minus" in variance.definition.lower()
    assert completion is not None and not completion.source_decisions, \
        "a decision about variance must not attach itself to an unrelated KPI"


def test_sign_convention_assumption_is_raised() -> None:
    spec = build_requirements(_payload(stated_kpis=["Budget Variance"]), None)
    assert any("sign convention" in a.statement.lower() for a in spec.assumptions)


def test_user_definition_overrides_the_catalogue() -> None:
    spec = build_requirements(
        _payload(business_definitions={"Total Cost": "Only invoiced cost, excluding accruals."}),
        None,
    )
    total = spec.kpi("Total Cost")
    assert total is not None and "accruals" in total.definition


def test_reporting_period_is_parsed() -> None:
    spec = build_requirements(_payload(reporting_period="FY2025 through FY2026"), None)
    assert spec.reporting_period_start == "2025-01-01"
    assert spec.reporting_period_end == "2026-12-31"


def test_time_intelligence_kpis_require_a_month_dimension() -> None:
    spec = build_requirements(_payload(stated_kpis=["Year-to-Date Cost"]), None)
    ytd = spec.kpi("Year-to-Date Cost")
    assert ytd is not None and "ytd" in ytd.time_intelligence
    assert "Month" in ytd.required_dimensions


def test_blocking_questions_are_surfaced() -> None:
    spec = build_requirements(_payload(stated_kpis=["Mystery Metric"]), None)
    assert spec.blocking_questions, "an undefined KPI must block measure generation"
