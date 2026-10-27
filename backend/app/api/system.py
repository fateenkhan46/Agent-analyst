"""System endpoints: capabilities, contracts, health.

``/capabilities`` is the single source of truth the frontend uses to decide
what to claim about Power BI integration -- so the UI can never advertise
something the backend cannot do.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..schemas import REQUIRED_CONTRACTS
from ..services.capabilities import capabilities, environment_summary

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/capabilities")
def get_capabilities() -> dict[str, object]:
    return {
        "capabilities": [c.model_dump(mode="json") for c in capabilities()],
        "environment": environment_summary(),
    }


@router.get("/contracts")
def get_contracts() -> dict[str, object]:
    """JSON Schema for every inter-agent contract.

    Serving these makes the agent boundaries inspectable, and the test suite
    snapshots this payload to catch an accidental breaking change.
    """
    import app.schemas as S

    out: dict[str, object] = {}
    for name in REQUIRED_CONTRACTS:
        model = getattr(S, name, None)
        if model is not None and hasattr(model, "model_json_schema"):
            out[name] = model.model_json_schema()
    return {"count": len(out), "contracts": out}


@router.get("/agents")
def get_agents() -> dict[str, object]:
    from ..agents.registry import agent_specs

    return {"agents": [a.model_dump(mode="json") for a in agent_specs()]}
