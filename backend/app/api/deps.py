"""Shared FastAPI dependencies."""
from __future__ import annotations

from fastapi import Depends
from sqlalchemy.orm import Session

from ..db import get_session
from ..services.auth import Principal, current_principal

__all__ = ["Depends", "Principal", "Session", "current_principal", "get_session"]
