"""Tools the export agent is allowed to call."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def register_tools() -> dict[str, Callable[..., Any]]:
    from .project import build_project, export_pbip, verify_project

    return {
        "build_pbip_project": build_project,
        "verify_pbip_project": verify_project,
        "export_pbip": export_pbip,
    }
