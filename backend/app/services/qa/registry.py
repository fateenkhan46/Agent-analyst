"""Tools the QA and improvement agents are allowed to call."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def register_tools() -> dict[str, Callable[..., Any]]:
    from .improver import suggest_improvements
    from .runner import run_qa

    return {"run_qa": run_qa, "suggest_improvements": suggest_improvements}
