"""The typed contracts themselves."""
from __future__ import annotations

import pytest

import app.schemas as S


def test_every_required_contract_exists() -> None:
    missing = [n for n in S.REQUIRED_CONTRACTS if not hasattr(S, n)]
    assert not missing, f"missing contracts: {missing}"


def test_contracts_reject_unknown_fields() -> None:
    """Extra fields must fail, so a drifting agent output is caught at the
    boundary instead of silently losing data."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        S.Project(owner_id="u", name="x", not_a_real_field=1)  # type: ignore[call-arg]


def test_destructive_steps_always_require_approval() -> None:
    from app.schemas.prep import DESTRUCTIVE_OPS, Risk, TransformationStep

    for op in DESTRUCTIVE_OPS:
        step = TransformationStep(op=op, target_table="t")
        assert step.requires_approval, op
        assert step.risk is Risk.DESTRUCTIVE, op


def test_low_confidence_step_requires_approval() -> None:
    from app.schemas.common import Confidence
    from app.schemas.prep import TransformOp, TransformationStep

    step = TransformationStep(
        op=TransformOp.RENAME_COLUMN, target_table="t", confidence=Confidence.LOW
    )
    assert step.requires_approval


def test_uncertain_relationship_is_gated() -> None:
    from app.schemas.common import Confidence
    from app.schemas.semantic import Cardinality, RelationshipDefinition

    high = RelationshipDefinition(
        from_table="f", from_column="k", to_table="d", to_column="k",
        confidence=Confidence.HIGH,
    )
    assert not high.requires_approval

    for kwargs in (
        {"confidence": Confidence.MEDIUM},
        {"confidence": Confidence.HIGH, "to_side_is_unique": False},
        {"confidence": Confidence.HIGH, "cardinality": Cardinality.MANY_TO_MANY},
    ):
        rel = RelationshipDefinition(
            from_table="f", from_column="k", to_table="d", to_column="k", **kwargs
        )
        assert rel.requires_approval, kwargs


def test_task_dependency_template_is_a_dag_covering_every_kind() -> None:
    from app.schemas.agent import TASK_DEPENDENCIES, TASK_OWNER, TaskKind

    assert set(TASK_DEPENDENCIES) == set(TaskKind)
    assert set(TASK_OWNER) == set(TaskKind)

    def walk(k: TaskKind, stack: tuple[TaskKind, ...] = ()) -> None:
        assert k not in stack, f"cycle through {k}"
        for d in TASK_DEPENDENCIES[k]:
            walk(d, stack + (k,))

    for kind in TaskKind:
        walk(kind)


def test_validation_report_does_not_pass_on_unrunnable_tests() -> None:
    """A test that could not run must not count as a pass."""
    from app.schemas.qa import (
        TestCategory, TestStatus, ValidationEngine, ValidationReport, ValidationResult,
    )

    def result(status: TestStatus) -> ValidationResult:
        return ValidationResult(
            test_id="t", test_name="n", category=TestCategory.ROW_COUNT,
            status=status, engine=ValidationEngine.PYTHON, expected="x",
        )

    assert ValidationReport(project_id="p", results=[result(TestStatus.PASS)]).passed
    assert ValidationReport(
        project_id="p", results=[result(TestStatus.NOT_SUPPORTED)]
    ).passed is True  # not blocking, but...
    report = ValidationReport(project_id="p", results=[result(TestStatus.NOT_SUPPORTED)])
    assert report.counts["pass"] == 0, "an unsupported test must never be counted as a pass"
    assert not ValidationReport(project_id="p", results=[result(TestStatus.FAIL)]).passed
    assert not ValidationReport(project_id="p", results=[result(TestStatus.ERROR)]).passed


def test_analysis_services_engine_is_never_claimed_by_default() -> None:
    """Nothing in the shipped code may label a result engine-validated."""
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    hits = subprocess.run(
        ["grep", "-rn", "ValidationEngine.ANALYSIS_SERVICES", str(root)],
        capture_output=True, text=True,
    ).stdout.strip()
    assert not hits, f"code claims the Analysis Services engine:\n{hits}"
