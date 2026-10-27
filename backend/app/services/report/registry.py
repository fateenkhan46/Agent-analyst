"""Tools the report design agent is allowed to call."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def register_tools() -> dict[str, Callable[..., Any]]:
    from .designer import design_report

    return {"design_report": design_report}
