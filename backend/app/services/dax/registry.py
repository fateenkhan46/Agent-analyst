"""Tools the DAX agent is allowed to call."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def register_tools() -> dict[str, Callable[..., Any]]:
    from .generator import generate_measures
    from .runtime import evaluate
    from .validator import evaluate_measure_result, validate_measure

    return {
        "generate_measures": generate_measures,
        "validate_measure": validate_measure,
        "evaluate_measure": evaluate,
        "evaluate_measure_result": evaluate_measure_result,
    }
