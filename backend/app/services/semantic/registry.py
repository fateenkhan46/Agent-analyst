"""Tools the semantic model agent is allowed to call."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def register_tools() -> dict[str, Callable[..., Any]]:
    from .designer import build_diagram, design_model

    return {"design_model": design_model, "build_diagram": build_diagram}
