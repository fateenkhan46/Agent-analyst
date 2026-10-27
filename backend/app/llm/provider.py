"""LLM provider abstraction.

Two providers implement one interface:

* ``AnthropicProvider`` -- the real thing, used when ``ANTHROPIC_API_KEY`` is
  set. Structured output is obtained through tool-use with a JSON schema, so
  the model's answer is schema-checked by the SDK before we see it, and then
  validated again against the Pydantic model.
* ``HeuristicProvider`` -- a deterministic, rule-based stand-in so the whole
  pipeline runs with no credentials. It is **not** a mock pretending to be a
  model: it never fabricates an answer. If a caller asks it for something it
  has no rule for, it raises ``NoHeuristicAvailable`` and the agent degrades
  explicitly rather than inventing content.

Every response carries ``origin`` so artifacts can be stamped truthfully, and
every call is costed and recorded.
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import get_settings
from ..schemas.agent import LlmCallRecord
from ..schemas.common import Money, Origin

T = TypeVar("T", bound=BaseModel)

#: USD per million tokens. Used for the cost ledger; update when pricing moves.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (15.0, 75.0),
    "claude-sonnet-5-5": (3.0, 15.0),
    "claude-haiku-5-5": (1.0, 5.0),
}
DEFAULT_PRICING = (3.0, 15.0)


class NoHeuristicAvailable(Exception):
    """The deterministic provider has no rule for this request.

    Raised rather than returning plausible-looking filler. Callers either
    handle it (and say so in the artifact) or fail the task.
    """


class LlmUnavailable(Exception):
    """A configured provider could not be reached."""


@dataclass
class StructuredRequest:
    """One request for a typed answer."""

    purpose: str
    system: str
    prompt: str
    output_model: type[BaseModel]
    #: Deterministic inputs the heuristic provider needs. Kept separate from
    #: the prose prompt so the two providers consume the same facts.
    facts: dict[str, Any] = field(default_factory=dict)
    max_output_tokens: int = 4096
    temperature: float = 0.0


@dataclass
class StructuredResponse:
    value: BaseModel
    origin: Origin
    record: LlmCallRecord


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICING.get(model, DEFAULT_PRICING)
    return round((input_tokens * pin + output_tokens * pout) / 1_000_000, 8)


class LlmProvider(ABC):
    name: str
    origin: Origin

    @abstractmethod
    def structured(self, request: StructuredRequest) -> StructuredResponse: ...

    @property
    def is_development_mode(self) -> bool:
        return self.origin is not Origin.LLM


class HeuristicProvider(LlmProvider):
    """Deterministic planner. Dispatches on ``purpose`` to a registered rule.

    Rules live with the agent that needs them (see
    ``app.llm.heuristics``), so each is unit-testable on its own and the
    provider stays a thin dispatcher.
    """

    name = "heuristic"
    origin = Origin.HEURISTIC

    def __init__(self) -> None:
        from .heuristics import HEURISTICS

        self._rules = HEURISTICS

    def structured(self, request: StructuredRequest) -> StructuredResponse:
        rule = self._rules.get(request.purpose)
        if rule is None:
            raise NoHeuristicAvailable(
                f"No deterministic rule exists for '{request.purpose}'. "
                f"Set ANTHROPIC_API_KEY to use a language model for this step. "
                f"Available rules: {', '.join(sorted(self._rules)) or 'none'}."
            )
        started = time.perf_counter()
        value = rule(request.facts, request.output_model)
        if not isinstance(value, request.output_model):
            value = request.output_model.model_validate(value)
        return StructuredResponse(
            value=value, origin=Origin.HEURISTIC,
            record=LlmCallRecord(
                provider=self.name, model="rule-based", purpose=request.purpose,
                prompt_chars=len(request.prompt),
                response_chars=len(value.model_dump_json()),
                cost=Money(),  # deterministic code costs nothing
                latency_ms=int((time.perf_counter() - started) * 1000),
                degraded_to_heuristic=True,
            ),
        )


class AnthropicProvider(LlmProvider):
    """Claude via tool-use for schema-enforced structured output."""

    name = "anthropic"
    origin = Origin.LLM

    def __init__(self, api_key: str, model: str, timeout: int) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise LlmUnavailable(f"the anthropic SDK is not installed: {exc}") from exc
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        self._model = model

    def structured(self, request: StructuredRequest) -> StructuredResponse:
        import anthropic

        schema = request.output_model.model_json_schema()
        tool = {
            "name": "emit_result",
            "description": f"Return the result for: {request.purpose}. "
                           f"Every field must be grounded in the supplied data.",
            "input_schema": _inline_refs(schema),
        }
        started = time.perf_counter()
        try:
            message = self._client.messages.create(
                model=self._model,
                max_tokens=request.max_output_tokens,
                temperature=request.temperature,
                system=request.system,
                tools=[tool],  # type: ignore[arg-type]
                tool_choice={"type": "tool", "name": "emit_result"},
                messages=[{"role": "user", "content": request.prompt}],
            )
        except (anthropic.APIConnectionError, anthropic.RateLimitError,
                anthropic.InternalServerError) as exc:
            # Classified transient so the agent harness retries.
            from ..agents.base import TransientError
            raise TransientError(f"Anthropic API unavailable: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LlmUnavailable(f"Anthropic API error {exc.status_code}: {exc.message}") from exc

        latency = int((time.perf_counter() - started) * 1000)
        payload: dict[str, Any] | None = None
        for block in message.content:
            if getattr(block, "type", "") == "tool_use":
                payload = dict(block.input)  # type: ignore[arg-type]
                break

        usage = message.usage
        cost = Money(
            amount_usd=price(self._model, usage.input_tokens, usage.output_tokens),
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        )
        record = LlmCallRecord(
            provider=self.name, model=self._model, purpose=request.purpose,
            prompt_chars=len(request.prompt), cost=cost, latency_ms=latency,
        )
        if payload is None:
            record.structured_output_valid = False
            record.error = "the model returned no structured result"
            raise LlmUnavailable(
                f"The model did not return a structured result for '{request.purpose}'."
            )
        try:
            value = request.output_model.model_validate(payload)
        except ValidationError as exc:
            record.structured_output_valid = False
            record.error = str(exc)[:600]
            # Surfaced as schema_invalid, which the policy retries once.
            raise
        record.response_chars = len(json.dumps(payload))
        return StructuredResponse(value=value, origin=Origin.LLM, record=record)


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline ``$defs``/``$ref`` so the schema is self-contained.

    Pydantic emits ``$ref`` pointers into ``$defs``; a tool input_schema is
    easiest to validate when it stands alone.
    """
    defs = schema.get("$defs", {})

    def walk(node: Any, seen: frozenset[str]) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                key = ref.split("/")[-1]
                if key in seen:
                    # Recursive type: degrade to a free object rather than
                    # recursing forever.
                    return {"type": "object"}
                target = defs.get(key, {})
                merged = walk(target, seen | {key})
                extra = {k: v for k, v in node.items() if k != "$ref"}
                return {**merged, **extra} if isinstance(merged, dict) else merged
            return {k: walk(v, seen) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v, seen) for v in node]
        return node

    out = walk(schema, frozenset())
    return out if isinstance(out, dict) else schema


_provider: LlmProvider | None = None


def get_provider() -> LlmProvider:
    """Resolve the configured provider. Falls back to heuristic *loudly* --
    the capability report and every artifact's provenance say so."""
    global _provider
    if _provider is not None:
        return _provider
    import os

    settings = get_settings()
    choice = settings.llm_provider
    key = settings.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if choice == "heuristic" or (choice == "auto" and not key):
        _provider = HeuristicProvider()
    else:
        if not key:
            raise LlmUnavailable(
                "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set."
            )
        _provider = AnthropicProvider(key, settings.llm_model, settings.llm_timeout_seconds)
    return _provider


def reset_provider() -> None:
    global _provider
    _provider = None


def ask(ctx: Any, request: StructuredRequest) -> StructuredResponse:
    """Call the provider through an ``AgentContext`` so the call is budgeted,
    costed and recorded. Agents always go through this, never the SDK."""
    provider = get_provider()
    if ctx.llm_calls_remaining <= 0:
        raise NoHeuristicAvailable(
            f"The LLM call budget for this agent is exhausted "
            f"({len(ctx.llm_calls)} calls used)."
        )
    response = provider.structured(request)
    ctx.record_llm(response.record)
    return response
