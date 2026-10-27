"""Tools the data preparation agent is allowed to call."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def register_tools() -> dict[str, Callable[..., Any]]:
    from .planner import plan_transformations
    from .runner import execute_plan, load_prepared_frames

    return {
        "plan_transformations": plan_transformations,
        "execute_plan": execute_plan,
        "load_prepared_frames": load_prepared_frames,
    }
