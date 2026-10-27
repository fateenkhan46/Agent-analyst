"""The agent execution harness.

Every agent is a subclass of ``Agent`` with:

* a declared ``spec`` -- purpose, typed input/output schemas, allowed tools,
  and an ``AgentPolicy`` (timeout, retries, cost cap, LLM call cap);
* a ``run(payload, ctx)`` method that receives a **validated** input model and
  returns an output model.

``execute`` wraps ``run`` with the policy: it validates both ends against the
declared schemas, enforces the timeout and the budget, retries on classified
transient failures, records an ``AgentExecution`` with every LLM and tool call,
and writes audit events. An agent that returns something not matching its
output schema fails with ``INVALID_OUTPUT`` -- it does not get to corrupt
downstream state.

The ``ToolBelt`` is a hard allowlist: an agent calling a tool outside its spec
raises. That is what makes "restricted toolset" a real constraint rather than
a comment.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..schemas.agent import (
    AgentExecution, AgentName, AgentSpec, AuditAction, ExecutionStatus,
    LlmCallRecord, ToolCallRecord,
)
from ..schemas.common import Contract, Money, Severity
from ..services import repository as repo

TIn = TypeVar("TIn", bound=Contract)
TOut = TypeVar("TOut", bound=Contract)


class ToolDenied(Exception):
    """An agent tried to use a tool outside its declared allowlist."""


class BudgetExceeded(Exception):
    """The agent's cost or call budget ran out."""


class AgentTimeout(Exception):
    pass


class TransientError(Exception):
    """Classified as retryable (network blip, provider 5xx, rate limit)."""


def digest(obj: Any) -> str:
    try:
        raw = json.dumps(obj, sort_keys=True, default=str).encode()
    except TypeError:
        raw = str(obj).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


class ToolBelt:
    """Mediates every side effect an agent performs.

    Tools are registered centrally; an agent only receives the ones its spec
    names. Each call is timed and recorded, so the audit log shows exactly
    which services an agent touched.
    """

    def __init__(self, allowed: list[str], registry: dict[str, Callable[..., Any]]) -> None:
        self._allowed = set(allowed)
        self._registry = registry
        self.calls: list[ToolCallRecord] = []

    def available(self) -> list[str]:
        return sorted(self._allowed & set(self._registry))

    def call(self, name: str, /, *args: Any, **kwargs: Any) -> Any:
        if name not in self._allowed:
            raise ToolDenied(
                f"Tool {name!r} is not in this agent's allowlist "
                f"({', '.join(sorted(self._allowed)) or 'none'})."
            )
        fn = self._registry.get(name)
        if fn is None:
            raise ToolDenied(f"Tool {name!r} is not registered.")
        started = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            self.calls.append(ToolCallRecord(
                name=name, arguments_digest=digest([args, kwargs]), succeeded=False,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            ))
            raise
        self.calls.append(ToolCallRecord(
            name=name, arguments_digest=digest([args, kwargs]), succeeded=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
        ))
        return result


@dataclass
class AgentContext:
    """Everything an agent may touch, handed to it explicitly."""

    session: Session
    project_id: str
    actor: str
    tools: ToolBelt
    policy_max_usd: float
    log: list[str] = field(default_factory=list)
    llm_calls: list[LlmCallRecord] = field(default_factory=list)
    spend: Money = field(default_factory=Money)
    _llm_call_budget: int = 4

    def note(self, message: str) -> None:
        self.log.append(message)

    def record_llm(self, record: LlmCallRecord) -> None:
        self.llm_calls.append(record)
        self.spend = self.spend + record.cost
        if self.spend.amount_usd > self.policy_max_usd:
            raise BudgetExceeded(
                f"LLM spend ${self.spend.amount_usd:.4f} exceeded the agent cap "
                f"of ${self.policy_max_usd:.2f}."
            )
        if len(self.llm_calls) > self._llm_call_budget:
            raise BudgetExceeded(
                f"{len(self.llm_calls)} LLM calls exceeded the cap of {self._llm_call_budget}."
            )

    @property
    def llm_calls_remaining(self) -> int:
        return max(0, self._llm_call_budget - len(self.llm_calls))


def _classify_error(exc: BaseException) -> str:
    if isinstance(exc, AgentTimeout):
        return "timeout"
    if isinstance(exc, TransientError):
        return "transient"
    if isinstance(exc, ValidationError):
        return "schema_invalid"
    if isinstance(exc, BudgetExceeded):
        return "budget"
    if isinstance(exc, ToolDenied):
        return "tool_denied"
    name = type(exc).__name__.lower()
    if any(w in name for w in ("timeout", "connection", "ratelimit", "overload")):
        return "transient"
    return "bug"


def _run_with_timeout(fn: Callable[[], Any], seconds: int) -> Any:
    """Run ``fn`` on a worker thread and abandon it after ``seconds``.

    A Python thread cannot be force-killed, so an abandoned call keeps running
    to completion in the background; what the timeout guarantees is that the
    orchestrator stops *waiting* and marks the attempt TIMED_OUT. Agent work is
    confined to its own ``AgentContext``, and the task result is only committed
    on the path that returned in time, so a late finisher cannot write over a
    newer attempt. Genuinely unbounded work (arbitrary generated code) is not
    run in this process at all -- see ``app.services.sandbox``.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True, name="agent-run")
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise AgentTimeout(f"Agent exceeded its {seconds}s timeout.")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class Agent(ABC, Generic[TIn, TOut]):
    """Base class. Subclasses declare ``spec`` and implement ``run``."""

    spec: AgentSpec
    input_model: type[TIn]
    output_model: type[TOut]

    @abstractmethod
    def run(self, payload: TIn, ctx: AgentContext) -> TOut:
        """Do the work. Raise to fail the attempt."""

    # -- harness --------------------------------------------------------
    def execute(
        self, s: Session, *, task_id: str, project_id: str, actor: str,
        payload: dict[str, Any] | TIn, attempt: int = 1,
    ) -> tuple[AgentExecution, TOut | None]:
        from .registry import tool_registry

        policy = self.spec.policy
        tools = ToolBelt(self.spec.allowed_tools, tool_registry())
        execution = AgentExecution(
            project_id=project_id, task_id=task_id, agent=self.spec.name, attempt=attempt,
            input_digest=digest(payload if isinstance(payload, dict) else payload.model_dump(mode="json")),
        )
        repo.save_execution(s, execution)
        repo.record_audit(
            s, project_id=project_id, actor=self.spec.name.value,
            action=AuditAction.AGENT_STARTED, execution_id=execution.id,
            summary=f"{self.spec.name.value} started (attempt {attempt}).", target=task_id,
            detail={"allowed_tools": tools.available()},
        )

        started = time.perf_counter()
        ctx = AgentContext(
            session=s, project_id=project_id, actor=actor, tools=tools,
            policy_max_usd=policy.max_usd,
        )
        ctx._llm_call_budget = policy.max_llm_calls

        # Budget check across the whole project, not just this agent.
        project = repo.get_project(s, project_id)
        if project:
            spent = repo.project_spend_usd(s, project_id)
            if spent >= project.budget_usd:
                execution.status = ExecutionStatus.BUDGET_EXCEEDED
                execution.error = (
                    f"Project spend ${spent:.4f} has reached its budget of "
                    f"${project.budget_usd:.2f}. Raise the budget to continue."
                )
                execution.error_kind = "budget"
                execution.finished_at = __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                )
                repo.save_execution(s, execution)
                repo.record_audit(
                    s, project_id=project_id, actor=self.spec.name.value,
                    action=AuditAction.AGENT_FAILED, severity=Severity.HIGH,
                    summary=execution.error, execution_id=execution.id,
                )
                return execution, None

        try:
            try:
                typed = payload if isinstance(payload, self.input_model) \
                    else self.input_model.model_validate(payload)
            except ValidationError as exc:
                raise ValidationError.from_exception_data(  # re-raise as-is
                    exc.title, exc.errors()  # type: ignore[arg-type]
                ) from exc

            result = _run_with_timeout(lambda: self.run(typed, ctx), policy.timeout_seconds)

            if not isinstance(result, self.output_model):
                # Validate rather than trust: an agent must honour its contract.
                result = self.output_model.model_validate(
                    result if isinstance(result, dict) else result.model_dump()
                )
            execution.status = ExecutionStatus.SUCCEEDED
            execution.output_digest = digest(result.model_dump(mode="json"))
        except BaseException as exc:  # noqa: BLE001 - recorded, then reported
            kind = _classify_error(exc)
            execution.error_kind = kind
            execution.error = f"{type(exc).__name__}: {exc}"
            execution.status = {
                "timeout": ExecutionStatus.TIMED_OUT,
                "schema_invalid": ExecutionStatus.INVALID_OUTPUT,
                "budget": ExecutionStatus.BUDGET_EXCEEDED,
            }.get(kind, ExecutionStatus.FAILED)
            if kind == "bug":
                execution.log.append(traceback.format_exc(limit=12))
            result = None
        finally:
            import datetime as _dt
            execution.duration_ms = int((time.perf_counter() - started) * 1000)
            execution.finished_at = _dt.datetime.now(_dt.timezone.utc)
            execution.llm_calls = ctx.llm_calls
            execution.tool_calls = tools.calls
            execution.cost = ctx.spend
            execution.log.extend(ctx.log)
            repo.save_execution(s, execution)

        if execution.status is ExecutionStatus.SUCCEEDED:
            repo.record_audit(
                s, project_id=project_id, actor=self.spec.name.value,
                action=AuditAction.AGENT_FINISHED, execution_id=execution.id,
                summary=f"{self.spec.name.value} succeeded in {execution.duration_ms} ms.",
                target=task_id,
                detail={"cost_usd": execution.cost.amount_usd,
                        "llm_calls": len(execution.llm_calls),
                        "tool_calls": len(execution.tool_calls)},
            )
        else:
            repo.record_audit(
                s, project_id=project_id, actor=self.spec.name.value,
                action=AuditAction.AGENT_FAILED, execution_id=execution.id,
                severity=Severity.HIGH,
                summary=f"{self.spec.name.value} failed ({execution.error_kind}): "
                        f"{execution.error[:240]}",
                target=task_id, detail={"error_kind": execution.error_kind},
            )
        return execution, result  # type: ignore[return-value]

    def execute_with_retries(
        self, s: Session, *, task_id: str, project_id: str, actor: str,
        payload: dict[str, Any] | TIn,
    ) -> tuple[AgentExecution, TOut | None]:
        """Apply the retry policy. Only error kinds listed in
        ``policy.retry_on`` are retried -- a logic bug is not retried, because
        re-running it would produce the same bug and burn budget."""
        policy = self.spec.policy
        attempt = 1
        last_exec: AgentExecution | None = None
        while True:
            execution, result = self.execute(
                s, task_id=task_id, project_id=project_id, actor=actor,
                payload=payload, attempt=attempt,
            )
            last_exec = execution
            if execution.status is ExecutionStatus.SUCCEEDED:
                return execution, result
            retryable = execution.error_kind in policy.retry_on
            if not retryable or attempt > policy.max_retries:
                return execution, None
            repo.record_audit(
                s, project_id=project_id, actor=self.spec.name.value,
                action=AuditAction.AGENT_RETRIED,
                summary=f"Retrying {self.spec.name.value} after a "
                        f"{execution.error_kind} failure (attempt {attempt + 1}).",
                target=task_id,
            )
            time.sleep(min(policy.retry_backoff_seconds * attempt, 5.0))
            attempt += 1
