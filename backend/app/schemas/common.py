"""Shared enums and base types for every inter-agent contract.

Everything that crosses an agent boundary is a Pydantic model defined under
``app.schemas``.  Agents never exchange free-form dicts: the orchestrator
validates each payload against the declared input/output schema before and
after an execution, so a malformed agent result fails loudly at the boundary
instead of corrupting downstream state.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Contract(BaseModel):
    """Base for every contract. Forbids unknown fields so a drifting agent
    output is rejected rather than silently losing data."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# --------------------------------------------------------------------------
# Confidence / provenance
# --------------------------------------------------------------------------
class Confidence(str, Enum):
    """How much the system trusts an inference. ``LOW`` always forces an
    approval gate rather than auto-apply."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Origin(str, Enum):
    """Where a piece of state came from. Used to keep LLM guesses separate
    from measured facts -- QA treats them differently."""

    DETERMINISTIC = "deterministic"   # computed from data by code
    LLM = "llm"                       # produced by a language model
    HEURISTIC = "heuristic"           # rule-based planner (LLM dev mode)
    USER = "user"                     # supplied or approved by a human
    IMPORTED = "imported"             # read from an existing artifact


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Provenance(Contract):
    origin: Origin
    produced_by: str = Field(description="Agent or service name.")
    confidence: Confidence = Confidence.MEDIUM
    rationale: str = ""
    evidence: list[str] = Field(
        default_factory=list,
        description="Concrete, checkable facts supporting the claim "
        "(e.g. 'cost.project_id: 100% of 412 values matched project.project_id').",
    )
    produced_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------
# Data typing
# --------------------------------------------------------------------------
class CanonicalType(str, Enum):
    """Our normalised type lattice. Maps to both Power BI data types and
    DuckDB SQL types (see ``app.services.dax.typemap``)."""

    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    CURRENCY = "currency"
    PERCENTAGE = "percentage"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    DURATION = "duration"
    IDENTIFIER = "identifier"
    MIXED = "mixed"
    EMPTY = "empty"

    @property
    def is_numeric(self) -> bool:
        return self in {
            CanonicalType.INTEGER,
            CanonicalType.DECIMAL,
            CanonicalType.CURRENCY,
            CanonicalType.PERCENTAGE,
            CanonicalType.DURATION,
        }

    @property
    def is_temporal(self) -> bool:
        return self in {CanonicalType.DATE, CanonicalType.DATETIME}


POWERBI_DATA_TYPE = {
    CanonicalType.STRING: "string",
    CanonicalType.IDENTIFIER: "string",
    CanonicalType.MIXED: "string",
    CanonicalType.EMPTY: "string",
    CanonicalType.INTEGER: "int64",
    CanonicalType.DECIMAL: "double",
    CanonicalType.CURRENCY: "decimal",
    CanonicalType.PERCENTAGE: "double",
    CanonicalType.DURATION: "double",
    CanonicalType.BOOLEAN: "boolean",
    CanonicalType.DATE: "dateTime",
    CanonicalType.DATETIME: "dateTime",
}

DUCKDB_TYPE = {
    CanonicalType.STRING: "VARCHAR",
    CanonicalType.IDENTIFIER: "VARCHAR",
    CanonicalType.MIXED: "VARCHAR",
    CanonicalType.EMPTY: "VARCHAR",
    CanonicalType.INTEGER: "BIGINT",
    CanonicalType.DECIMAL: "DOUBLE",
    CanonicalType.CURRENCY: "DECIMAL(18,4)",
    CanonicalType.PERCENTAGE: "DOUBLE",
    CanonicalType.DURATION: "DOUBLE",
    CanonicalType.BOOLEAN: "BOOLEAN",
    CanonicalType.DATE: "DATE",
    CanonicalType.DATETIME: "TIMESTAMP",
}


# --------------------------------------------------------------------------
# Capability reporting -- the honesty layer
# --------------------------------------------------------------------------
class CapabilityStatus(str, Enum):
    IMPLEMENTED = "implemented"            # built here and covered by tests
    NOT_CONFIGURED = "not_configured"      # built, needs credentials/env
    UNAVAILABLE = "unavailable"            # cannot work in this environment
    NOT_IMPLEMENTED = "not_implemented"    # deliberately out of MVP scope

    @property
    def usable(self) -> bool:
        return self is CapabilityStatus.IMPLEMENTED


class Capability(Contract):
    key: str
    title: str
    status: CapabilityStatus
    detail: str
    requires: list[str] = Field(default_factory=list)
    tested: bool = False


class Money(Contract):
    amount_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Money") -> "Money":
        return Money(
            amount_usd=round(self.amount_usd + other.amount_usd, 8),
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class Issue(Contract):
    """A single machine-checkable finding. Used by the profiler, the prep
    engine, the model validator and QA alike so the UI renders one shape."""

    code: str
    severity: Severity
    message: str
    location: str = Field(default="", description="table / table.column / measure name")
    evidence: dict[str, Any] = Field(default_factory=dict)
    recommended_fix: str = ""
