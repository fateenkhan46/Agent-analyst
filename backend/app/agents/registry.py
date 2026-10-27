"""Agent and tool registries.

The tool registry is the complete set of side-effecting functions any agent may
be granted. Nothing else is reachable through a ``ToolBelt``, so the union of
all ``allowed_tools`` across the specs is the system's entire agent-driven
capability surface -- auditable in one place.
"""
from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any

from ..schemas.agent import AgentName, AgentSpec


@lru_cache(maxsize=1)
def tool_registry() -> dict[str, Callable[..., Any]]:
    from ..services import repository as repo
    from ..services.profiling.service import load_frames, profile_project

    registry: dict[str, Callable[..., Any]] = {
        # profiling
        "profile_project": profile_project,
        "load_frames": load_frames,
        # read-only state access
        "load_artifact": repo.load_artifact,
        "list_source_files": repo.list_source_files,
        "list_datasets": repo.list_datasets,
        "get_dataset": repo.get_dataset,
        "get_project": repo.get_project,
        # writes
        "save_artifact": repo.save_artifact,
        "save_dataset": repo.save_dataset,
        "save_approval": repo.save_approval,
        "record_audit": repo.record_audit,
    }
    # Later phases register their own tools; see the modules' register_tools().
    for module_path in (
        "app.services.prep.registry",
        "app.services.semantic.registry",
        "app.services.dax.registry",
        "app.services.pbip.registry",
        "app.services.qa.registry",
    ):
        try:
            mod = __import__(module_path, fromlist=["register_tools"])
        except ImportError:
            continue
        register = getattr(mod, "register_tools", None)
        if register:
            registry.update(register())
    return registry


@lru_cache(maxsize=1)
def _agents() -> dict[AgentName, Any]:
    from .profiling_agent import DataProfilingAgent

    instances: list[Any] = [DataProfilingAgent()]
    for module_path, cls_name in (
        ("app.agents.requirements_agent", "RequirementsMeetingAgent"),
        ("app.agents.prep_agent", "DataPreparationAgent"),
        ("app.agents.semantic_agent", "SemanticModelAgent"),
        ("app.agents.dax_agent", "DaxAgent"),
        ("app.agents.report_agent", "ReportDesignAgent"),
        ("app.agents.qa_agent", "QaAgent"),
        ("app.agents.improvement_agent", "ImprovementAgent"),
    ):
        try:
            mod = __import__(module_path, fromlist=[cls_name])
        except ImportError:
            continue
        cls = getattr(mod, cls_name, None)
        if cls:
            instances.append(cls())
    return {a.spec.name: a for a in instances}


def get_agent(name: AgentName) -> Any:
    agent = _agents().get(name)
    if agent is None:
        raise KeyError(
            f"Agent {name.value!r} is not implemented yet. "
            f"Available: {', '.join(sorted(a.value for a in _agents()))}."
        )
    return agent


def agent_specs() -> list[AgentSpec]:
    return [a.spec for a in _agents().values()]


def reset_registries() -> None:
    tool_registry.cache_clear()
    _agents.cache_clear()
