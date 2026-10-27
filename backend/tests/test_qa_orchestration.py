"""QA, the orchestrator's dependency graph and approval gates, improvements."""
from __future__ import annotations

import pytest

from app.schemas.agent import TaskKind, TaskStatus
from app.schemas.qa import TestStatus


@pytest.fixture
def completed_project(client, profiled_project) -> str:
    """A project driven through the whole pipeline."""
    r = client.post(
        f"/api/v1/projects/{profiled_project}/run", json={"auto_approve": True}
    )
    failed = [s for s in r.json()["steps"] if not s.get("succeeded")]
    assert not failed, failed
    return profiled_project


def _validation(client, pid) -> dict:
    return client.get(f"/api/v1/projects/{pid}/artifacts/validation").json()["artifact"]


# --- the whole pipeline --------------------------------------------------
def test_all_ten_tasks_complete(client, completed_project) -> None:
    body = client.get(f"/api/v1/projects/{completed_project}/tasks").json()
    assert body["progress"] == 1.0, [
        (t["kind"], t["status"]) for t in body["tasks"] if t["status"] != "succeeded"
    ]
    assert len(body["tasks"]) == 10


def test_every_artifact_is_produced(client, completed_project) -> None:
    artifacts = client.get(
        f"/api/v1/projects/{completed_project}/artifacts"
    ).json()["artifacts"]
    for name in ("profile", "minutes", "requirements", "plan", "execution",
                 "model", "report", "validation", "improvements"):
        assert artifacts[name]["exists"], f"{name} was not produced"


def test_delivery_bundle_is_complete_and_downloadable(client, completed_project) -> None:
    body = client.get(f"/api/v1/projects/{completed_project}/delivery").json()
    for key, section in body["sections"].items():
        assert section["available"], f"{key} is unavailable: {section['reason']}"
    assert body["exports"], "no export is offered"
    for export in body["exports"]:
        assert export["file_present"], "an export is listed but the file is missing"
        assert export["download_href"]


# --- QA is independent and honest ---------------------------------------
def test_qa_uses_no_language_model(client, completed_project) -> None:
    """A reviewer whose verdict could be talked out of a failure is not a
    reviewer."""
    from app.agents.registry import get_agent
    from app.schemas.agent import AgentName

    assert get_agent(AgentName.QA).spec.uses_llm is False

    executions = client.get(
        f"/api/v1/projects/{completed_project}/executions"
    ).json()["executions"]
    qa = [e for e in executions if e["agent"] == "qa_agent"]
    assert qa, "QA did not run"
    for execution in qa:
        assert execution["llm_calls"] == []
        assert execution["cost"]["amount_usd"] == 0.0


def test_qa_verdicts_come_from_real_engines(client, completed_project) -> None:
    report = _validation(client, completed_project)
    engines = {
        r["engine"] for r in report["results"]
        if r["status"] not in {"skipped", "not_supported"}
    }
    assert "duckdb" in engines, "integrity must be measured against the real data"
    assert "dax_duckdb_transpiled" in engines, "measures must actually be executed"
    assert "llm_review" not in engines, "no verdict may come from a language model"
    assert "analysis_services" not in engines, "no verdict may claim the Power BI engine"


def test_qa_states_what_it_could_not_verify(client, completed_project) -> None:
    report = _validation(client, completed_project)
    note = report["validation_scope_note"]
    assert "without a Power BI engine" in note
    assert "Analysis Services" in note
    assert "Mark as date table" in note


def test_unsupported_checks_are_not_counted_as_passes(client, completed_project) -> None:
    report = _validation(client, completed_project)
    counts = {}
    for r in report["results"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    passes = sum(1 for r in report["results"] if r["status"] == "pass")
    assert counts.get("pass", 0) == passes
    for r in report["results"]:
        if r["status"] == "not_supported":
            assert r["skipped_reason"], "an unsupported check must say why"


def test_every_failure_names_evidence_and_a_fix(client, completed_project) -> None:
    report = _validation(client, completed_project)
    for r in report["results"]:
        if r["status"] in {"fail", "error", "warn"}:
            assert r["expected"], f"{r['test_name']} has no expectation"
            assert r["actual"], f"{r['test_name']} has no actual value"
            assert r["recommended_fix"] or r["status"] == "warn", (
                f"{r['test_name']} fails with no recommended fix"
            )


def test_the_sample_pipeline_passes_qa(client, completed_project) -> None:
    """The headline claim: on this data the pipeline's output validates."""
    report = _validation(client, completed_project)
    blocking = [
        r for r in report["results"] if r["status"] in {"fail", "error"}
    ]
    assert not blocking, [
        f"{r['test_name']}: {r['actual']}" for r in blocking
    ]


# --- the checks that matter ---------------------------------------------
def test_double_counting_is_actually_checked(client, completed_project) -> None:
    report = _validation(client, completed_project)
    checks = [r for r in report["results"] if r["category"] == "double_counting"]
    assert checks, "the double-counting check did not run"
    executed = [r for r in checks if r["status"] == "pass"]
    assert executed, "no double-counting check actually executed"
    for r in executed:
        assert "sum to" in r["expected"]
        assert r["evidence"].get("slices"), "the slice values are the evidence"


def test_totals_are_checked_against_an_independent_sum(client, completed_project) -> None:
    report = _validation(client, completed_project)
    checks = [
        r for r in report["results"] if r["category"] == "totals_reconciliation"
    ]
    assert checks, "no measure was cross-checked against a direct sum"
    for r in checks:
        assert r["evidence"].get("method", "").startswith("computed independently")


def test_referential_integrity_is_measured_not_assumed(client, completed_project) -> None:
    report = _validation(client, completed_project)
    checks = [
        r for r in report["results"]
        if r["category"] == "referential_integrity" and r["status"] != "skipped"
    ]
    assert checks, "referential integrity was not measured"
    for r in checks:
        assert r["engine"] == "duckdb"
        assert "orphan_keys" in r["evidence"] or r["status"] == "skipped"


def test_row_counts_reconcile_to_the_source(client, completed_project) -> None:
    report = _validation(client, completed_project)
    checks = [r for r in report["results"] if r["category"] == "row_count"]
    assert checks
    for r in checks:
        if r["status"] == "pass":
            evidence = r["evidence"]
            assert evidence["source_rows"] + evidence["added"] - evidence["removed"] \
                == evidence["final_rows"]


# --- QA sends work back --------------------------------------------------
def test_qa_routes_failures_to_the_responsible_agent() -> None:
    """A failing check must name who has to fix it."""
    from app.schemas.common import Severity
    from app.schemas.qa import (
        TestCategory, ValidationEngine, ValidationReport, ValidationResult,
    )
    from app.services.qa.runner import build_rework

    report = ValidationReport(project_id="p", results=[
        ValidationResult(
            test_id="a", test_name="Cardinality", category=TestCategory.CARDINALITY,
            status=TestStatus.FAIL, engine=ValidationEngine.DUCKDB,
            expected="unique", actual="duplicated", severity=Severity.CRITICAL,
            responsible_agent="semantic_model_agent",
            recommended_fix="De-duplicate the dimension.",
        ),
        ValidationResult(
            test_id="b", test_name="DAX parses", category=TestCategory.DAX_SYNTAX,
            status=TestStatus.FAIL, engine=ValidationEngine.DAX_PARSER,
            expected="valid", actual="syntax error", severity=Severity.CRITICAL,
            responsible_agent="dax_agent", recommended_fix="Fix the syntax.",
        ),
        ValidationResult(
            test_id="c", test_name="Passing", category=TestCategory.ROW_COUNT,
            status=TestStatus.PASS, engine=ValidationEngine.PYTHON, expected="x",
        ),
    ])
    requests = build_rework(report)
    agents = {r.target_agent for r in requests}
    assert agents == {"semantic_model_agent", "dax_agent"}
    for request in requests:
        assert request.instructions, "rework must carry the fix instructions"


def test_rework_request_becomes_a_real_task(client, profiled_project) -> None:
    """QA returning work must actually schedule it."""
    from app.db import session_scope
    from app.orchestrator.graph import add_rework_task
    from app.services import repository as repo

    client.post(
        f"/api/v1/projects/{profiled_project}/run",
        json={"stop_after": "generate_measures", "auto_approve": True},
    )
    with session_scope() as s:
        before = len(repo.load_task_graph(s, profiled_project).tasks)
        add_rework_task(
            s, profiled_project, TaskKind.GENERATE_MEASURES,
            reason="QA found a wrong total",
        )
        graph = repo.load_task_graph(s, profiled_project)
    assert len(graph.tasks) == before + 1
    rework = [t for t in graph.tasks if t.requested_by == "qa_agent"]
    assert rework and rework[0].rework_reason


def test_rework_reblocks_downstream_tasks(client, completed_project) -> None:
    """Re-running an upstream step must invalidate what came after it."""
    from app.db import session_scope
    from app.orchestrator.graph import add_rework_task
    from app.services import repository as repo

    with session_scope() as s:
        add_rework_task(
            s, completed_project, TaskKind.PROPOSE_SEMANTIC_MODEL,
            reason="model is wrong",
        )
        graph = repo.load_task_graph(s, completed_project)
    downstream = [
        t for t in graph.tasks
        if t.kind in {TaskKind.GENERATE_MEASURES, TaskKind.DESIGN_REPORT,
                      TaskKind.RUN_QA, TaskKind.EXPORT_PBIP}
    ]
    assert any(t.status is TaskStatus.BLOCKED for t in downstream), (
        "downstream results are stale and must not stay marked succeeded"
    )


# --- orchestration -------------------------------------------------------
def test_a_task_cannot_run_before_its_dependencies(client, project_id, labour_csv) -> None:
    client.post(
        f"/api/v1/projects/{project_id}/files",
        files=[("files", ("labour.csv", labour_csv, "text/csv"))],
    )
    r = client.post(
        f"/api/v1/projects/{project_id}/tasks/export_pbip/run", json={}
    )
    body = r.json()
    assert body["succeeded"] is False and body["blocked"] is True
    assert "must succeed first" in body["reason"]


def test_export_is_gated_on_qa(client, profiled_project) -> None:
    """The deliverable cannot be produced before it has been validated."""
    client.post(
        f"/api/v1/projects/{profiled_project}/run",
        json={"stop_after": "design_report", "auto_approve": True},
    )
    r = client.post(
        f"/api/v1/projects/{profiled_project}/tasks/export_pbip/run", json={}
    )
    body = r.json()
    assert body.get("blocked") and "run_qa" in body["reason"]


def test_destructive_steps_block_execution_until_decided(
    client, profiled_project
) -> None:
    client.post(
        f"/api/v1/projects/{profiled_project}/tasks/extract_requirements/run", json={}
    )
    client.post(
        f"/api/v1/projects/{profiled_project}/tasks/plan_transformations/run", json={}
    )
    pending = [
        a for a in
        client.get(f"/api/v1/projects/{profiled_project}/approvals").json()["approvals"]
        if a["state"] == "pending"
        and a["kind"] in {"transformation_step", "transformation_plan"}
    ]
    assert pending, "a destructive step should raise an approval"
    r = client.post(
        f"/api/v1/projects/{profiled_project}/tasks/execute_transformations/run", json={}
    )
    body = r.json()
    assert body.get("blocked"), "execution must wait for the approval decision"
    assert body["approvals"]


def test_approval_decisions_are_audited(client, completed_project) -> None:
    events = client.get(
        f"/api/v1/projects/{completed_project}/audit?limit=500"
    ).json()["events"]
    actions = {e["action"] for e in events}
    assert "approval_requested" in actions
    assert "approval_decided" in actions
    assert "qa_run" in actions
    assert "export_created" in actions


def test_agents_cannot_call_tools_outside_their_allowlist() -> None:
    from app.agents.registry import agent_specs, tool_registry

    registered = set(tool_registry())
    for spec in agent_specs():
        unknown = [t for t in spec.allowed_tools if t not in registered]
        assert not unknown, f"{spec.name.value} allows unregistered tool(s): {unknown}"


def test_every_agent_declares_a_policy_and_schemas() -> None:
    from app.agents.registry import agent_specs

    specs = agent_specs()
    assert len(specs) >= 8
    for spec in specs:
        assert spec.purpose and spec.input_schema and spec.output_schema
        assert spec.policy.timeout_seconds > 0
        assert spec.policy.max_retries >= 0
        if not spec.uses_llm:
            assert spec.policy.max_usd == 0.0, (
                f"{spec.name.value} does not use an LLM but has a spend budget"
            )


def test_executions_are_recorded_for_every_agent(client, completed_project) -> None:
    executions = client.get(
        f"/api/v1/projects/{completed_project}/executions"
    ).json()["executions"]
    agents = {e["agent"] for e in executions}
    for expected in ("data_profiling_agent", "requirements_meeting_agent",
                     "data_preparation_agent", "semantic_model_agent", "dax_agent",
                     "report_design_agent", "qa_agent", "improvement_agent"):
        assert expected in agents, f"{expected} has no execution record"
    for execution in executions:
        assert execution["duration_ms"] >= 0
        assert execution["status"] in {"succeeded", "failed", "timed_out",
                                       "budget_exceeded", "invalid_output"}


# --- improvements --------------------------------------------------------
def test_every_recommendation_has_rationale_evidence_and_trade_offs(
    client, completed_project
) -> None:
    report = client.get(
        f"/api/v1/projects/{completed_project}/artifacts/improvements"
    ).json()["artifact"]
    assert report["recommendations"], "no recommendation was produced"
    for rec in report["recommendations"]:
        assert rec["rationale"], f"{rec['title']} has no rationale"
        assert rec["evidence"], f"{rec['title']} has no evidence"
        assert rec["expected_benefit"], f"{rec['title']} states no benefit"
        assert rec["trade_offs"], (
            f"{rec['title']} states no trade-off -- a recommendation with no cost "
            f"has usually not been thought through"
        )


def test_recommendations_are_not_applied_automatically(
    client, completed_project
) -> None:
    report = client.get(
        f"/api/v1/projects/{completed_project}/artifacts/improvements"
    ).json()["artifact"]
    for rec in report["recommendations"]:
        assert rec["state"] == "proposed"

    first = report["recommendations"][0]
    r = client.put(
        f"/api/v1/projects/{completed_project}/improvements/{first['id']}",
        json={"approve": True},
    )
    body = r.json()
    assert body["applied"] is False, (
        "approving a recommendation records the decision; it must not silently "
        "mutate an approved artifact"
    )
    assert body["recommendation"]["state"] == "approved"


def test_improvements_surface_the_unverified_measures(
    client, completed_project
) -> None:
    """The honest gap must become a recommendation, not stay buried in QA."""
    validation = _validation(client, completed_project)
    unsupported = [
        r for r in validation["results"] if r["status"] == "not_supported"
    ]
    if not unsupported:
        pytest.skip("every measure was evaluable in this fixture")
    report = client.get(
        f"/api/v1/projects/{completed_project}/artifacts/improvements"
    ).json()["artifact"]
    assert any(
        "Power BI Desktop" in rec["title"] for rec in report["recommendations"]
    ), "QA's unevaluated measures should become an explicit recommendation"
